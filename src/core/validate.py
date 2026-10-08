"""Validate a SOLVED schedule for hard-constraint conflicts.

This is the "find conflicts in a schedule" module (not ``conflicts.py``,
which builds the pre-solve demand matrix). Checks the CURRENT meeting
assignments of a schedule (as they stand after any manual edits) for:
- room double-bookings (same room, overlapping meeting times),
- faculty overlaps (same instructor in two places at once),
- student overlaps (a student enrolled in two sections that meet at the
  same time).

Cross-listed packs (same ``crosslist_group`` and section number) are one
physical class and are never reported as conflicting with each other.

Two meetings "overlap" when their time slots share at least one day and
their clock times intersect.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from sqlmodel import Session, select

from src.core.timeutil import slots_overlap
from src.database.models import (
    Course,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

logger = logging.getLogger(__name__)


@dataclass
class ConflictSection:
    id: int
    crn: str
    course_code: str
    section_number: str


@dataclass
class Conflict:
    kind: str  # "room" | "faculty" | "student"
    detail: str
    sections: list[ConflictSection]
    affected_students: int = 0


@dataclass
class ConflictReport:
    total: int = 0
    room: int = 0
    faculty: int = 0
    student: int = 0
    conflicts: list[Conflict] = field(default_factory=list)


def check_schedule_conflicts(session: Session, schedule_id: int) -> ConflictReport:
    """Find hard-constraint conflicts in a schedule's current assignments."""
    report = ConflictReport()

    sections = list(
        session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
    )
    if not sections:
        return report
    section_by_id = {s.id: s for s in sections}
    section_ids = list(section_by_id.keys())

    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
    meetings = list(
        session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id.in_(section_ids))
        ).all()
    )

    codes = {s.course_code for s in sections}
    group_of = {
        c.code: c.crosslist_group
        for c in session.exec(select(Course).where(Course.code.in_(codes))).all()
    }

    def pack_key(section_id: int):
        s = section_by_id[section_id]
        group = group_of.get(s.course_code)
        return (group, s.section_number) if group else None

    def same_pack(a: int, b: int) -> bool:
        key = pack_key(a)
        return key is not None and key == pack_key(b)

    def cs(section_id: int) -> ConflictSection:
        s = section_by_id[section_id]
        return ConflictSection(
            id=s.id, crn=s.crn, course_code=s.course_code, section_number=s.section_number
        )

    def slot_label(slot: TimeSlot) -> str:
        return f"{slot.pattern} {slot.start_time}-{slot.end_time}"

    # ── Room double-bookings ──────────────────────────────────────────
    # meetings grouped by room → pairwise slot overlap
    by_room: dict[str, list[SectionMeeting]] = defaultdict(list)
    for m in meetings:
        by_room[m.room_id].append(m)
    seen_room: set[tuple] = set()
    for room_id, ms in by_room.items():
        for m1, m2 in combinations(ms, 2):
            if m1.section_id == m2.section_id or same_pack(m1.section_id, m2.section_id):
                continue
            s1, s2 = slots.get(m1.time_slot_id), slots.get(m2.time_slot_id)
            if not s1 or not s2 or not slots_overlap(s1, s2):
                continue
            key = (room_id, *sorted((m1.section_id, m2.section_id)), s1.id, s2.id)
            if key in seen_room:
                continue
            seen_room.add(key)
            report.conflicts.append(
                Conflict(
                    kind="room",
                    detail=f"Room {room_id} double-booked · {slot_label(s1)}",
                    sections=[cs(m1.section_id), cs(m2.section_id)],
                )
            )
            report.room += 1

    # ── Faculty overlaps ──────────────────────────────────────────────
    meetings_by_section: dict[int, list[SectionMeeting]] = defaultdict(list)
    for m in meetings:
        meetings_by_section[m.section_id].append(m)

    by_faculty: dict[str, list[int]] = defaultdict(list)
    for s in sections:
        if s.faculty_id:
            by_faculty[s.faculty_id].append(s.id)
    for faculty_id, sids in by_faculty.items():
        for sid1, sid2 in combinations(sids, 2):
            if same_pack(sid1, sid2):
                continue
            conflict_slot = _first_overlap(
                meetings_by_section[sid1], meetings_by_section[sid2], slots
            )
            if conflict_slot is None:
                continue
            report.conflicts.append(
                Conflict(
                    kind="faculty",
                    detail=(
                        f"Instructor {faculty_id} teaches two sections · "
                        f"{slot_label(conflict_slot)}"
                    ),
                    sections=[cs(sid1), cs(sid2)],
                )
            )
            report.faculty += 1

    # ── Student overlaps ──────────────────────────────────────────────
    enrollments = list(
        session.exec(
            select(SectionEnrollment).where(
                SectionEnrollment.section_id.in_(section_ids)
            )
        ).all()
    )
    sections_by_student: dict[str, list[int]] = defaultdict(list)
    for e in enrollments:
        sections_by_student[e.student_id].append(e.section_id)

    # Count affected students per conflicting section-pair.
    pair_students: dict[tuple[int, int], int] = defaultdict(int)
    pair_slot: dict[tuple[int, int], TimeSlot] = {}
    for sids in sections_by_student.values():
        if len(sids) < 2:
            continue
        for sid1, sid2 in combinations(sorted(set(sids)), 2):
            if same_pack(sid1, sid2):
                continue
            pair = (sid1, sid2)
            if pair in pair_students:
                pair_students[pair] += 1
                continue
            conflict_slot = _first_overlap(
                meetings_by_section[sid1], meetings_by_section[sid2], slots
            )
            if conflict_slot is None:
                continue
            pair_students[pair] = 1
            pair_slot[pair] = conflict_slot
    for pair, count in pair_students.items():
        if pair not in pair_slot:
            continue
        sid1, sid2 = pair
        report.conflicts.append(
            Conflict(
                kind="student",
                detail=f"{count} student(s) enrolled in both · {slot_label(pair_slot[pair])}",
                sections=[cs(sid1), cs(sid2)],
                affected_students=count,
            )
        )
        report.student += 1

    report.total = report.room + report.faculty + report.student
    logger.info(
        "Schedule %s conflicts: %d room, %d faculty, %d student",
        schedule_id, report.room, report.faculty, report.student,
    )
    return report


def _first_overlap(
    ms1: list[SectionMeeting], ms2: list[SectionMeeting], slots: dict[int, TimeSlot]
) -> TimeSlot | None:
    """Return the first overlapping slot between two sections' meetings."""
    for m1 in ms1:
        s1 = slots.get(m1.time_slot_id)
        if not s1:
            continue
        for m2 in ms2:
            s2 = slots.get(m2.time_slot_id)
            if s2 and slots_overlap(s1, s2):
                return s1
    return None
