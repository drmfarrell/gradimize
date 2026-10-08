#!/usr/bin/env python3
"""Student-free schedule report — the safe way to inspect a solve's OUTPUT.

Run logs (src/core/runlog.py) cover what happened DURING a run; this covers
what came out: the section grid, per-section enrollment COUNTS, per-course
demand-vs-seats coverage, room/slot utilization, aggregate student-experience
metrics, and solver stats. Everything is section-level or aggregate — no
student ids, no rosters — so the report is safe for an AI assistant to read
and safe to email. Canary-enforced by tests/test_run_report.py.

The report is written into the run-log directory (GRADIMIZE_LOG_DIR →
GRADIMIZE_DATA_DIR/logs → repo logs/) as report-schedule-<id>.md, stable
name so re-running overwrites rather than accumulating.

Usage:
    python scripts/run_report.py            # latest schedule
    python scripts/run_report.py 12         # specific schedule id
    python scripts/run_report.py --list     # list schedules (id/name/status)

Respects GRADIMIZE_DATA_DIR like every other entry point.
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session, func, select

from src.database.models import (
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    StudentDemand,
    TimeSlot,
)

# Deliberately NOT imported: Student, StudentCourseHistory. This module has
# no reason to touch a student-level table beyond COUNT/GROUP BY aggregates,
# and keeping the imports out makes a violating diff visible at the top.


def build_report(session: Session, schedule_id: int) -> str:
    """Render the report as markdown. Raises ValueError on unknown id."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if schedule is None:
        raise ValueError(f"Schedule {schedule_id} not found")

    sections = list(session.exec(
        select(Section).where(Section.schedule_id == schedule_id)).all())
    section_ids = [s.id for s in sections]

    meetings_by_sid: dict[int, list[SectionMeeting]] = defaultdict(list)
    if section_ids:
        for m in session.exec(
            select(SectionMeeting)
            .where(SectionMeeting.section_id.in_(section_ids))
            .order_by(SectionMeeting.meeting_sequence)
        ).all():
            meetings_by_sid[m.section_id].append(m)

    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
    rooms = {r.id: r for r in session.exec(select(Room)).all()}

    # Enrollment COUNTS only — the group-by never surfaces a student id.
    enrolled_by_sid: dict[int, int] = {}
    if section_ids:
        enrolled_by_sid = dict(session.exec(
            select(SectionEnrollment.section_id, func.count())
            .where(SectionEnrollment.section_id.in_(section_ids))
            .group_by(SectionEnrollment.section_id)
        ).all())

    demand_by_course: dict[str, int] = dict(session.exec(
        select(StudentDemand.course_code, func.count())
        .group_by(StudentDemand.course_code)
    ).all())

    lines: list[str] = []
    out = lines.append
    out(f"# Schedule report — {schedule.name} (id {schedule_id})")
    out("")
    out(f"- Semester: {schedule.semester}  ·  Status: {schedule.status}  ·  "
        f"Created: {schedule.created_at}")
    stats = schedule.solver_stats or {}
    if stats:
        gap = stats.get("optimality_gap_pct")
        out(f"- Solver: status={stats.get('status')}, "
            f"objective={stats.get('objective_value')}, "
            f"gap={f'{gap}%' if gap is not None else 'n/a'}, "
            f"time={stats.get('solve_time_seconds')}s")
    total_enrolled = sum(enrolled_by_sid.values())
    out(f"- {len(sections)} sections, {total_enrolled} enrollments (count)")
    out("")

    # ── Section grid ─────────────────────────────────────────────────
    out("## Sections")
    out("")
    out("| Course | Sec | CRN | Meeting(s) | Room(s) | Instructor | Enrolled/Cap |")
    out("|---|---|---|---|---|---|---|")
    for s in sorted(sections, key=lambda x: (x.course_code, x.section_number)):
        mts = meetings_by_sid.get(s.id, [])
        slot_txt = "; ".join(
            f"{slots[m.time_slot_id].name}" if m.time_slot_id in slots
            else f"slot#{m.time_slot_id}" for m in mts) or "—"
        room_txt = "; ".join(
            rooms[m.room_id].id if m.room_id in rooms else str(m.room_id)
            for m in mts) or "—"
        n = enrolled_by_sid.get(s.id, 0)
        flag = " ⚠OVER" if n > s.capacity else ""
        pin = " 📌" if s.is_pinned else ""
        out(f"| {s.course_code} | {s.section_number}{pin} | {s.crn} "
            f"| {slot_txt} | {room_txt} | {s.faculty_id or '—'} "
            f"| {n}/{s.capacity}{flag} |")
    out("")

    # ── Demand coverage per course ──────────────────────────────────
    out("## Demand coverage (per course)")
    out("")
    out("| Course | Demands | Sections | Seats | Enrolled | Unmet |")
    out("|---|---|---|---|---|---|")
    seats_by_course: dict[str, int] = defaultdict(int)
    sections_by_course: dict[str, int] = defaultdict(int)
    enrolled_by_course: dict[str, int] = defaultdict(int)
    for s in sections:
        seats_by_course[s.course_code] += s.capacity
        sections_by_course[s.course_code] += 1
        enrolled_by_course[s.course_code] += enrolled_by_sid.get(s.id, 0)
    courses = sorted(set(demand_by_course) | set(seats_by_course))
    total_unmet = 0
    for code in courses:
        d = demand_by_course.get(code, 0)
        e = enrolled_by_course.get(code, 0)
        unmet = max(0, d - e)
        total_unmet += unmet
        mark = " ⚠" if unmet else ""
        out(f"| {code} | {d} | {sections_by_course.get(code, 0)} "
            f"| {seats_by_course.get(code, 0)} | {e} | {unmet}{mark} |")
    out("")
    out(f"Total unmet demand: {total_unmet}")
    out("")

    # ── Utilization ─────────────────────────────────────────────────
    out("## Utilization")
    out("")
    slot_use: dict[str, int] = defaultdict(int)
    room_use: dict[str, int] = defaultdict(int)
    for mts in meetings_by_sid.values():
        for m in mts:
            if m.time_slot_id in slots:
                slot_use[slots[m.time_slot_id].name] += 1
            room_use[str(m.room_id)] += 1
    busiest_slots = sorted(slot_use.items(), key=lambda kv: -kv[1])[:10]
    out("Busiest slots: " + (", ".join(f"{k} ({v})" for k, v in busiest_slots)
                             or "none"))
    busiest_rooms = sorted(room_use.items(), key=lambda kv: -kv[1])[:10]
    out("")
    out("Busiest rooms: " + (", ".join(f"{k} ({v})" for k, v in busiest_rooms)
                             or "none"))
    idle_rooms = [r.id for r in rooms.values()
                  if r.is_active and str(r.id) not in room_use]
    out("")
    out(f"Active rooms with zero meetings: {len(idle_rooms)}"
        + (f" ({', '.join(sorted(idle_rooms)[:15])}"
           + ("…" if len(idle_rooms) > 15 else "") + ")" if idle_rooms else ""))
    out("")

    # ── Student experience — AGGREGATES ONLY ────────────────────────
    # analyze_student_quality also returns worst_students (per-student
    # rows). That field is deliberately never serialized here.
    out("## Student experience (aggregates)")
    out("")
    try:
        from src.core.schedule_quality import analyze_student_quality

        q = analyze_student_quality(session, schedule_id)
        out(f"- Students with a schedule: {q.total_students}")
        out(f"- Days on campus: mean {q.mean_days_on_campus:.2f}, "
            f"median {q.median_days_on_campus:.1f}; "
            f"{q.students_5_day} students at 5 days")
        out(f"- Mean gap between classes: {q.mean_gap_minutes:.0f} min; "
            f"{q.students_big_gap} students with a 3h+ gap")
        out(f"- {q.students_no_lunch} without a lunch break; "
            f"{q.students_long_workday} with a long workday; "
            f"{q.students_heavy_marathon} with 3+ back-to-back classes")
    except Exception as e:  # aggregate metrics are nice-to-have, not vital
        out(f"- Quality metrics unavailable ({type(e).__name__})")
    out("")
    out(f"_Generated {datetime.now().isoformat(timespec='seconds')} — "
        f"student-data-free by construction (see tests/test_run_report.py)._")
    out("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a student-data-free schedule report (section grid, "
            "enrollment counts, demand coverage, utilization, aggregate "
            "student-experience metrics). With no arguments, reports on "
            "the latest schedule."
        ))
    parser.add_argument(
        "schedule_id", nargs="?", type=int, default=None,
        help="Schedule id to report on (default: latest schedule)")
    parser.add_argument(
        "--schedule-id", dest="schedule_id_opt", type=int, default=None,
        help="Schedule id to report on (alternative to the positional arg)")
    parser.add_argument(
        "--list", action="store_true",
        help="List schedules (id/name/status) instead of generating a report")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    from src.core.runlog import runlog_dir
    from src.database.connection import create_tables, get_engine

    args = parse_args(argv)

    engine = get_engine()
    create_tables(engine)
    with Session(engine) as session:
        if args.list:
            for sv in session.exec(select(ScheduleVersion)).all():
                print(f"{sv.id:>4}  {sv.semester}  {sv.status:<10} {sv.name}")
            return 0

        schedule_id = args.schedule_id_opt if args.schedule_id_opt is not None else args.schedule_id
        if schedule_id is None:
            schedule_id = session.exec(
                select(func.max(ScheduleVersion.id))).one()
            if schedule_id is None:
                print("No schedules in the database.")
                return 1

        report = build_report(session, schedule_id)

    out_dir = runlog_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report-schedule-{schedule_id}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"Report (student-data-free): {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
