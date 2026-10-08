"""Bulk student-schedule export for mass distribution.

`advisor.student_sheet` and `ical.ics_for_student` each render one
student's schedule at a time — fine for an advising appointment, not for
handing every enrolled student their schedule at once. This module writes
that bulk artifact set for a whole `ScheduleVersion` in one pass: a single
CSV of every (enrollment, meeting) row plus, optionally, a per-student
markdown sheet and a per-student .ics calendar, all keyed by the
student's pseudonym (this server never sees real identities). The
registrar-side `scripts/reidentify.py` tool swaps pseudonyms back to real
IDs before the files are actually mailed out.

Read-only: nothing here adds or commits to the session.
"""

import csv
import logging
from collections import defaultdict
from pathlib import Path

from sqlmodel import Session, select

from src.core.advisor import student_sheet
from src.core.ical import ics_for_student
from src.database.models import (
    Course,
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

logger = logging.getLogger(__name__)

DAY_ORDER = ["M", "T", "W", "R", "F", "S", "U"]

CSV_FIELDS = [
    "student_id", "course_code", "course_title", "crn", "section_number",
    "meeting_days", "start_time", "end_time", "room_id", "instructor_name",
]


def _fmt_days(days: list[str]) -> str:
    return "".join(d for d in DAY_ORDER if d in days)


def export_student_schedules(
    session: Session,
    schedule_id: int,
    out_dir: str | Path,
    include_sheets: bool = True,
    include_ical: bool = True,
) -> dict:
    """Write bulk per-student distribution artifacts for `schedule_id`.

    Under `out_dir`:
      - `student_schedules.csv` — one row per (enrollment, meeting); a
        section with zero meetings gets one row with blank meeting/room
        fields. Deterministic ordering by (student_id, course_code,
        section_number, meeting_sequence).
      - `sheets/<student_id>.md` — `advisor.student_sheet` output, one per
        enrolled student (only when `include_sheets`).
      - `ical/<student_id>.ics` — `ical.ics_for_student` output, one per
        enrolled student (only when `include_ical`).

    Read-only with respect to the database. Raises ValueError if
    `schedule_id` does not exist.

    Returns:
        {students, enrollments, csv_rows, sheets_written, ics_written,
         sections_without_meetings}
    """
    schedule = session.get(ScheduleVersion, schedule_id)
    if schedule is None:
        raise ValueError(f"Schedule {schedule_id} not found")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sections = list(
        session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
    )
    sections_by_id = {s.id: s for s in sections if s.id is not None}
    section_ids = list(sections_by_id)

    enrollments = list(
        session.exec(
            select(SectionEnrollment).where(
                SectionEnrollment.section_id.in_(section_ids)  # type: ignore[union-attr]
            )
        ).all()
    ) if section_ids else []

    courses = {
        c.code: c
        for c in session.exec(
            select(Course).where(
                Course.code.in_({s.course_code for s in sections})  # type: ignore[union-attr]
            )
        ).all()
    } if sections else {}
    faculty = {f.id: f for f in session.exec(select(Faculty)).all()}
    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
    rooms = {r.id: r for r in session.exec(select(Room)).all()}

    meetings_by_sid: dict[int, list[SectionMeeting]] = defaultdict(list)
    if section_ids:
        for m in session.exec(
            select(SectionMeeting).where(
                SectionMeeting.section_id.in_(section_ids)  # type: ignore[union-attr]
            )
        ).all():
            meetings_by_sid[m.section_id].append(m)
    for lst in meetings_by_sid.values():
        lst.sort(key=lambda m: m.meeting_sequence)

    rows: list[dict] = []
    sections_without_meetings: set[int] = set()
    student_ids: set[str] = set()
    for e in enrollments:
        student_ids.add(e.student_id)
        section = sections_by_id.get(e.section_id)
        if section is None:
            continue
        course = courses.get(section.course_code)
        fac = faculty.get(section.faculty_id) if section.faculty_id else None
        base = {
            "student_id": e.student_id,
            "course_code": section.course_code,
            "course_title": course.title if course else "",
            "crn": section.crn,
            "section_number": section.section_number,
            "instructor_name": fac.name if fac else "",
        }
        section_meetings = meetings_by_sid.get(section.id, [])
        if not section_meetings:
            sections_without_meetings.add(section.id)
            rows.append({
                **base,
                "meeting_days": "", "start_time": "", "end_time": "", "room_id": "",
            })
            continue
        for m in section_meetings:
            slot = slots.get(m.time_slot_id)
            room = rooms.get(m.room_id)
            rows.append({
                **base,
                "meeting_days": _fmt_days(slot.days) if slot else "",
                "start_time": slot.start_time if slot else "",
                "end_time": slot.end_time if slot else "",
                "room_id": room.id if room else m.room_id,
            })

    rows.sort(key=lambda r: (
        r["student_id"], r["course_code"], r["section_number"],
        r["start_time"], r["room_id"],
    ))

    csv_path = out_dir / "student_schedules.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    sheets_written = 0
    ics_written = 0
    if include_sheets:
        sheets_dir = out_dir / "sheets"
        sheets_dir.mkdir(parents=True, exist_ok=True)
        for sid in sorted(student_ids):
            (sheets_dir / f"{sid}.md").write_text(
                student_sheet(session, schedule_id, sid), encoding="utf-8"
            )
            sheets_written += 1
    if include_ical:
        ical_dir = out_dir / "ical"
        ical_dir.mkdir(parents=True, exist_ok=True)
        for sid in sorted(student_ids):
            (ical_dir / f"{sid}.ics").write_text(
                ics_for_student(session, schedule_id, sid), encoding="utf-8"
            )
            ics_written += 1

    return {
        "students": len(student_ids),
        "enrollments": len(enrollments),
        "csv_rows": len(rows),
        "sheets_written": sheets_written,
        "ics_written": ics_written,
        "sections_without_meetings": len(sections_without_meetings),
    }
