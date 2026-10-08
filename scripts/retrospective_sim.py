#!/usr/bin/env python3
"""Retrospective simulation: compare Gradimize schedule against an actual historical schedule.

Loads historical data, runs the solver to produce a counterfactual schedule,
then compares the two on key metrics:
  - Graduation-critical course access
  - Schedule conflict count
  - Room utilization
  - Total enrollment satisfaction

Usage:
    python scripts/retrospective_sim.py \
        --actual-schedule data/actual_schedule.csv \
        [--degreeworks data/audit_course_needed.csv] \
        [--time-limit 120]

The actual schedule CSV is the data-request schedule_<term>.csv layout
(exports/Gradimize_Data_Request.md §6):
    CRN, SUBJECT + COURSE_NUMBER (or COURSE_CODE), MEETING_DAYS,
    START_TIME, END_TIME, BUILDING, ROOM, ACTUAL_ENROLLMENT
Legacy column names (COURSE_CODE, ENROLLMENT/ENROLLED, ROOM_NUMBER) are
also accepted.
"""

import argparse
import csv
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session, select

from src.core.conflicts import TimeSlotOverlapCalculator, build_conflict_matrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.database.connection import create_tables, get_engine
from src.database.models import (
    Room,
    StudentDemand,
    TimeSlot,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)


def load_actual_schedule(csv_path: Path) -> list[dict]:
    """Load the actual historical schedule CSV."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            reader.fieldnames = [h.strip().upper().replace(" ", "_") for h in reader.fieldnames]
        return list(reader)


def match_slot(row: dict, time_slots: list[TimeSlot]) -> int | None:
    """Match a CSV row to a TimeSlot by days + start/end time."""
    days = row.get("MEETING_DAYS", "").strip()
    start = row.get("START_TIME", "").strip()
    end = row.get("END_TIME", "").strip()
    if not days or not start or not end:
        return None
    day_set = set(days)
    for slot in time_slots:
        if set(slot.days) == day_set and slot.start_time == start and slot.end_time == end:
            return slot.id
    return None


def match_room(row: dict, rooms: list[Room]) -> str | None:
    """Match a CSV row to a Room by building + room number (or room id)."""
    building = row.get("BUILDING", "").strip()
    number = row.get("ROOM", row.get("ROOM_NUMBER", "")).strip()
    if not building or not number:
        return None
    for room in rooms:
        if room.building == building and room.number == number:
            return room.id
    # Fallback: Banner building CODE + number is the room id convention
    # (SCI + 104 -> SCI-104), covering extracts whose rooms.csv building
    # column holds the long description instead of the code.
    rid = f"{building}-{number}".upper()
    for room in rooms:
        if room.id.upper() == rid:
            return room.id
    return None


def compute_actual_metrics(
    actual_rows: list[dict],
    demands: list[StudentDemand],
    time_slots: list[TimeSlot],
    rooms: list[Room],
    slot_overlaps: dict[int, set[int]],
) -> dict:
    """Compute metrics for the actual schedule."""
    # Build actual schedule structure
    actual_sections: dict[str, dict] = {}  # crn -> {course_code, slot_id, room_id, enrollment}
    for row in actual_rows:
        crn = row.get("CRN", "").strip()
        if not crn:
            continue
        course_code = row.get("COURSE_CODE", "").strip().replace(" ", "")
        if not course_code:
            subj = row.get("SUBJECT", "").strip()
            num = row.get("COURSE_NUMBER", "").strip()
            course_code = f"{subj}{num}"
        slot_id = match_slot(row, time_slots)
        room_id = match_room(row, rooms)
        enrollment_raw = (
            row.get("ACTUAL_ENROLLMENT")
            or row.get("ENROLLMENT")
            or row.get("ENROLLED")
            or "0"
        )
        enrollment = int(enrollment_raw.strip() or "0")
        actual_sections[crn] = {
            "course_code": course_code,
            "slot_id": slot_id,
            "room_id": room_id,
            "enrollment": enrollment,
        }

    # Courses offered in the actual schedule
    actual_courses = {s["course_code"] for s in actual_sections.values()}

    # Graduation-critical access. "Offered" (course exists in the schedule)
    # is the only thing knowable for the actual schedule — real enrollment
    # rosters for it aren't in this dataset. The Gradimize side reports the
    # same "offered" number for an apples-to-apples comparison, plus its
    # actual enrollments separately.
    grad_critical = [d for d in demands if d.is_graduation_critical]
    grad_served = sum(1 for d in grad_critical if d.course_code in actual_courses)
    grad_total = len(grad_critical)

    # Schedule conflict count (promised in the module docstring): pairs of
    # sections in overlapping slots whose courses share demanding students.
    demand_students = defaultdict(set)
    for d in demands:
        demand_students[d.course_code].add(d.student_id)
    placed = [
        (s["course_code"], s["slot_id"])
        for s in actual_sections.values()
        if s["slot_id"] is not None
    ]
    conflict_pairs = 0
    for i, (c1, sl1) in enumerate(placed):
        for c2, sl2 in placed[i + 1 :]:
            if c1 == c2:
                continue
            if sl1 == sl2 or sl2 in slot_overlaps.get(sl1, set()):
                if demand_students[c1] & demand_students[c2]:
                    conflict_pairs += 1

    # Room utilization
    used_room_slots: set[tuple] = set()
    all_room_ids = {r.id for r in rooms}
    all_slot_ids = {s.id for s in time_slots}
    for s in actual_sections.values():
        if s["room_id"] and s["slot_id"]:
            used_room_slots.add((s["room_id"], s["slot_id"]))
    total_room_slots = len(all_room_ids) * len(all_slot_ids)

    # Room distribution (sections per room)
    room_counts = defaultdict(int)
    for s in actual_sections.values():
        if s["room_id"]:
            room_counts[s["room_id"]] += 1

    # Total enrollment
    total_enrollment = sum(s["enrollment"] for s in actual_sections.values())

    # Demand satisfaction (courses offered that match demands)
    demand_by_course = defaultdict(int)
    for d in demands:
        demand_by_course[d.course_code] += 1
    demands_with_sections = sum(
        count for cc, count in demand_by_course.items() if cc in actual_courses
    )

    return {
        "sections": len(actual_sections),
        "grad_critical_total": grad_total,
        "grad_critical_served": grad_served,
        "conflict_pairs": conflict_pairs,
        "room_slots_used": len(used_room_slots),
        "room_slots_total": total_room_slots,
        "room_util_pct": (len(used_room_slots) / total_room_slots * 100) if total_room_slots else 0,
        "rooms_used": len(room_counts),
        "total_enrollment": total_enrollment,
        "demands_with_sections": demands_with_sections,
        "demands_total": len(demands),
    }


def compute_gradimize_metrics(
    result,
    demands: list[StudentDemand],
    rooms: list[Room],
    time_slots: list[TimeSlot],
    slot_overlaps: dict[int, set[int]],
) -> dict:
    """Compute metrics for the Gradimize-generated schedule."""
    sections = result.sections or []
    enrollments = result.enrollments or []
    meetings = result.meetings or []

    # Courses offered
    gradimize_courses = {s.course_code for s in sections}

    # Graduation-critical access — two measures:
    # "served" = actually ENROLLED (Gradimize knows its rosters), and
    # "offered" = course present, matching what the actual schedule reports.
    grad_critical = [d for d in demands if d.is_graduation_critical]
    grad_offered = sum(1 for d in grad_critical if d.course_code in gradimize_courses)
    section_by_id = {s.id: s for s in sections}
    grad_enrolled = set()
    for e in enrollments:
        sec = section_by_id.get(e.section_id)
        if sec:
            for d in grad_critical:
                if d.student_id == e.student_id and d.course_code == sec.course_code:
                    grad_enrolled.add((d.student_id, d.course_code))
    grad_served = len(grad_enrolled)

    # Room utilization from meetings
    used_room_slots: set[tuple] = set()
    room_counts = defaultdict(int)
    for m in meetings:
        used_room_slots.add((m.room_id, m.time_slot_id))
        room_counts[m.room_id] += 1

    total_room_slots = len({r.id for r in rooms}) * len({s.id for s in time_slots})

    # Demand satisfaction
    demand_by_course = defaultdict(int)
    for d in demands:
        demand_by_course[d.course_code] += 1
    demands_with_sections = sum(
        count for cc, count in demand_by_course.items() if cc in gradimize_courses
    )

    # Conflict count on the same definition as the actual schedule
    demand_students = defaultdict(set)
    for d in demands:
        demand_students[d.course_code].add(d.student_id)
    slot_by_section = {m.section_id: m.time_slot_id for m in meetings
                       if m.meeting_sequence == 0}
    placed = [
        (s.course_code, slot_by_section[s.id])
        for s in sections if s.id in slot_by_section
    ]
    conflict_pairs = 0
    for i, (c1, sl1) in enumerate(placed):
        for c2, sl2 in placed[i + 1 :]:
            if c1 == c2:
                continue
            if sl1 == sl2 or sl2 in slot_overlaps.get(sl1, set()):
                if demand_students[c1] & demand_students[c2]:
                    conflict_pairs += 1

    return {
        "sections": len(sections),
        "grad_critical_total": len(grad_critical),
        "grad_critical_served": grad_served,
        "grad_critical_offered": grad_offered,
        "conflict_pairs": conflict_pairs,
        "room_slots_used": len(used_room_slots),
        "room_slots_total": total_room_slots,
        "room_util_pct": (len(used_room_slots) / total_room_slots * 100) if total_room_slots else 0,
        "rooms_used": len(room_counts),
        "total_enrollment": len(enrollments),
        "demands_with_sections": demands_with_sections,
        "demands_total": len(demands),
    }


# Rows shown in both the console table and the markdown report.
# "Grad-critical offered" is the apples-to-apples row: for the actual
# schedule "offered" (course present) is the only knowable measure, so
# Gradimize reports its own "offered" beside it — its true enrolled count
# is a separate Gradimize-only row.
_COMPARISON_ROWS = [
    ("Sections scheduled", "sections", "sections"),
    ("Total enrollment", "total_enrollment", "total_enrollment"),
    ("Graduation-critical demands", "grad_critical_total", "grad_critical_total"),
    ("Conflicting section pairs", "conflict_pairs", "conflict_pairs"),
    ("Grad-critical offered", "grad_critical_served", "grad_critical_offered"),
    ("Grad-critical enrolled (Gradimize)", None, "grad_critical_served"),
    ("Room-slot pairs used", "room_slots_used", "room_slots_used"),
    ("Room utilization %", "room_util_pct", "room_util_pct"),
    ("Distinct rooms used", "rooms_used", "rooms_used"),
    ("Demands with offered sections", "demands_with_sections", "demands_with_sections"),
    ("Total demands", "demands_total", "demands_total"),
]


def _improvement_lines(actual: dict, gradimize: dict) -> list[str]:
    lines = []
    gc_t = actual["grad_critical_total"]
    if gc_t > 0:
        gc_a = actual["grad_critical_served"]  # offered-based (see above)
        gc_c = gradimize["grad_critical_offered"]
        lines.append(
            f"Grad-critical offered: {gc_a}/{gc_t} ({gc_a/gc_t*100:.1f}%) -> "
            f"{gc_c}/{gc_t} ({gc_c/gc_t*100:.1f}%)"
        )
        gc_e = gradimize["grad_critical_served"]
        lines.append(
            f"Grad-critical actually enrolled by Gradimize: {gc_e}/{gc_t} "
            f"({gc_e/gc_t*100:.1f}%)"
        )

    enr_a = actual["total_enrollment"]
    enr_c = gradimize["total_enrollment"]
    if enr_a > 0:
        delta = enr_c - enr_a
        lines.append(
            f"Total enrollment: {enr_a} -> {enr_c} ({delta:+d}, {delta/enr_a*100:+.1f}%)"
        )

    cf_a = actual["conflict_pairs"]
    cf_c = gradimize["conflict_pairs"]
    lines.append(f"Conflicting section pairs: {cf_a} -> {cf_c} ({cf_c - cf_a:+d})")
    lines.append(
        f"Room utilization: {actual['room_util_pct']:.1f}% -> "
        f"{gradimize['room_util_pct']:.1f}%"
    )
    return lines


def print_comparison(actual: dict, gradimize: dict):
    """Print a formatted comparison table."""
    print()
    print("=" * 70)
    print("RETROSPECTIVE SIMULATION: Actual vs. Gradimize Schedule")
    print("=" * 70)
    print()
    print(f"{'Metric':<40} {'Actual':>12} {'Gradimize':>12}")
    print("-" * 70)

    for label, a_key, c_key in _COMPARISON_ROWS:
        av = actual[a_key] if a_key else None
        cv = gradimize[c_key]
        if isinstance(cv, float):
            a_txt = f"{av:.1f}%" if av is not None else "—"
            print(f"{label:<40} {a_txt:>12} {cv:>11.1f}%")
        else:
            a_txt = str(av) if av is not None else "—"
            print(f"{label:<40} {a_txt:>12} {cv:>12}")

    print()
    print("Improvements:")
    for line in _improvement_lines(actual, gradimize):
        print(f"  {line}")
    print()


def write_markdown_report(
    path: Path,
    actual: dict,
    gradimize: dict,
    semester: str,
    actual_source: str,
    solver_status: str,
    solve_seconds: float,
    n_demands: int,
):
    """Write the comparison as a shareable markdown report."""
    lines = [
        f"# Schedule back-test — {semester}",
        "",
        f"Gradimize rebuilt the {semester} schedule from scratch (same courses,",
        "rooms, instructors, and student demand) and the result is compared",
        "against the actual schedule below. The optimizer never saw the actual",
        "schedule — it worked only from demand.",
        "",
        f"- Actual schedule: `{actual_source}`",
        f"- Demand rows: {n_demands}",
        f"- Solver: {solver_status} in {solve_seconds:.0f}s",
        "",
        "| Metric | Actual | Gradimize |",
        "|---|---:|---:|",
    ]
    for label, a_key, c_key in _COMPARISON_ROWS:
        av = actual[a_key] if a_key else None
        cv = gradimize[c_key]
        if isinstance(cv, float):
            a_txt = f"{av:.1f}%" if av is not None else "—"
            c_txt = f"{cv:.1f}%"
        else:
            a_txt = str(av) if av is not None else "—"
            c_txt = str(cv)
        lines.append(f"| {label} | {a_txt} | {c_txt} |")

    lines += ["", "## Headline changes", ""]
    lines += [f"- {line}" for line in _improvement_lines(actual, gradimize)]
    lines += [
        "",
        "*\"Offered\" counts a graduation-critical demand as met when the needed",
        "course appears in the schedule (the only measure knowable for the",
        "actual schedule); Gradimize's enrolled row counts students actually",
        "seated. \"Conflicting section pairs\" counts same-time section pairs",
        "whose courses share at least one demanding student.*",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown report written to {path}")


def main():
    parser = argparse.ArgumentParser(description="Gradimize Retrospective Simulation")
    parser.add_argument("--actual-schedule", required=True,
                        help="CSV of the actual historical schedule")
    parser.add_argument("--degreeworks", default=None,
                        help="DegreeWorks audit CSV (optional)")
    parser.add_argument("--demand-source", choices=["degreeworks", "internal"],
                        default=None)
    parser.add_argument("--time-limit", type=int, default=120)
    parser.add_argument("--config", default=None, help="Scheduler YAML config")
    parser.add_argument("--semester", default="2025SP",
                        help="Semester being rebuilt, e.g. 2026FA (drives "
                             "off-cadence gating and labels)")
    parser.add_argument("--report-md", default=None,
                        help="Also write the comparison as a markdown report")
    args = parser.parse_args()

    engine = get_engine()
    create_tables(engine)

    with Session(engine) as session:
        config = SchedulerConfig(args.config)
        config.solver_settings["max_time_seconds"] = args.time_limit
        config.solver_settings["num_workers"] = 0
        if args.demand_source:
            config.demand_source = args.demand_source
        if args.degreeworks:
            config.demand_source = "degreeworks"
            config.degreeworks_csv = args.degreeworks

        # Ensure demands exist
        demand_count = len(list(session.exec(select(StudentDemand)).all()))
        if demand_count == 0:
            from src.core.graph import PrerequisiteGraph
            graph = PrerequisiteGraph(session)

            if config.demand_source == "degreeworks" and config.degreeworks_csv:
                dw_path = Path(config.degreeworks_csv)
                if dw_path.exists():
                    from src.core.degreeworks import DegreeWorksImporter
                    print(f"Importing DegreeWorks audit: {dw_path}")
                    importer = DegreeWorksImporter(session, graph)
                    importer.import_audit_needs(dw_path)
                else:
                    print("DegreeWorks CSV not found, using internal calculator")
                    from src.core.demand import DemandCalculator
                    DemandCalculator(session, graph).calculate_all_demands()
            else:
                from src.core.demand import DemandCalculator
                print("Calculating demands (internal)...")
                DemandCalculator(session, graph).calculate_all_demands()

        demands = list(session.exec(select(StudentDemand)).all())
        time_slots = list(session.exec(select(TimeSlot)).all())
        rooms = list(session.exec(select(Room)).all())

        overlap_calc = TimeSlotOverlapCalculator(session)
        slot_overlaps = overlap_calc.compute_overlaps()

        # Load actual schedule
        actual_path = Path(args.actual_schedule)
        if not actual_path.exists():
            print(f"Error: actual schedule not found: {actual_path}", file=sys.stderr)
            sys.exit(1)
        actual_rows = load_actual_schedule(actual_path)
        print(f"Loaded actual schedule: {len(actual_rows)} rows from {actual_path}")

        # Compute actual metrics
        actual_metrics = compute_actual_metrics(
            actual_rows, demands, time_slots, rooms, slot_overlaps
        )

        # Run Gradimize solver
        print(f"Running Gradimize solver for {args.semester}...")
        conflict_matrix = build_conflict_matrix(session)
        scheduler = SchedulingEngine(session, conflict_matrix, config)
        sections = scheduler.create_sections(args.semester)
        result = scheduler.solve(
            sections, f"Retrospective Sim {args.semester}", args.semester
        )

        if result.status not in ("optimal", "feasible"):
            print(f"Solver returned {result.status}. Cannot compare.", file=sys.stderr)
            for w in result.staffing_warnings:
                print(f"  WHY: {w}", file=sys.stderr)
            sys.exit(1)

        # Compute Gradimize metrics
        gradimize_metrics = compute_gradimize_metrics(
            result, demands, rooms, time_slots, slot_overlaps
        )

        # Print comparison
        print_comparison(actual_metrics, gradimize_metrics)

        if args.report_md:
            write_markdown_report(
                Path(args.report_md),
                actual_metrics,
                gradimize_metrics,
                semester=args.semester,
                actual_source=str(actual_path),
                solver_status=result.status,
                solve_seconds=result.solve_time_seconds,
                n_demands=len(demands),
            )


if __name__ == "__main__":
    main()
