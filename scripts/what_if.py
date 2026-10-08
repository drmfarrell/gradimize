#!/usr/bin/env python3
"""What-if scenario runner: resource tweaks vs. baseline (see src/core/scenarios.py).

Usage:
    python scripts/what_if.py --semester 2026FA \
        --add-faculty "FNEW:Prof New:CHEM101,CHEM201:12" \
        --add-room "SCI-301:Science Building:301:40:LECTURE" \
        [--remove-faculty FID] [--remove-room RID] [--set-cap ENGL101:20] \
        [--time-limit 120] [--report exports/whatif_2026FA.md]

Runs two full solver passes against THROWAWAY COPIES of the live database
(GRADIMIZE_DATA_DIR respected) — the live data is never modified. Budget
roughly 2x --time-limit seconds of wall time (one solve per side). All of
--add-faculty/--remove-faculty/--add-room/--remove-room/--set-cap are
repeatable.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.scenarios import (
    METRIC_ROWS,
    ScenarioTweaks,
    format_metric_delta,
    format_metric_value,
    run_scenario,
    write_scenario_report,
)
from src.database.connection import DEFAULT_DB_PATH
from src.database.models import RoomType


def _split(spec: str, n: int, usage: str) -> list[str]:
    parts = spec.split(":")
    if len(parts) != n:
        raise argparse.ArgumentTypeError(f"expected {usage}, got {spec!r}")
    return parts


def _parse_add_faculty(spec: str) -> dict:
    fid, name, courses_raw, max_credits = _split(
        spec, 4, "ID:NAME:COURSE1,COURSE2:MAX_CREDITS"
    )
    try:
        max_credits_int = int(max_credits)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--add-faculty max_credits must be an integer, got {max_credits!r}"
        ) from None
    courses = [c.strip() for c in courses_raw.split(",") if c.strip()]
    return {
        "id": fid.strip(),
        "name": name.strip(),
        "approved_courses": courses,
        "max_credits": max_credits_int,
    }


def _parse_add_room(spec: str) -> dict:
    rid, building, number, capacity, room_type_raw = _split(
        spec, 5, "ID:BUILDING:NUMBER:CAPACITY:ROOM_TYPE"
    )
    try:
        capacity_int = int(capacity)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--add-room capacity must be an integer, got {capacity!r}"
        ) from None
    try:
        room_type = RoomType(room_type_raw.strip().upper())
    except ValueError:
        valid = ", ".join(t.value for t in RoomType)
        raise argparse.ArgumentTypeError(
            f"--add-room room_type must be one of: {valid} (got {room_type_raw!r})"
        ) from None
    return {
        "id": rid.strip(),
        "building": building.strip(),
        "number": number.strip(),
        "capacity": capacity_int,
        "room_type": room_type.value,
    }


def _parse_set_cap(spec: str) -> tuple[str, int]:
    code, cap = _split(spec, 2, "COURSE:CAP")
    try:
        cap_int = int(cap)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--set-cap cap must be an integer, got {cap!r}"
        ) from None
    return code.strip(), cap_int


def main() -> int:
    ap = argparse.ArgumentParser(description="Gradimize what-if scenario runner")
    ap.add_argument("--semester", required=True, help="Semester to solve, e.g. 2026FA")
    ap.add_argument("--time-limit", type=int, default=120,
                    help="Solver seconds per side (default 120; solves twice)")
    ap.add_argument("--add-faculty", action="append", default=[], type=_parse_add_faculty,
                    metavar="ID:NAME:COURSES:MAX_CREDITS",
                    help='Repeatable, e.g. "FNEW:Prof New:CHEM101,CHEM201:12"')
    ap.add_argument("--remove-faculty", action="append", default=[], metavar="ID",
                    help="Repeatable faculty id to remove from the scenario copy")
    ap.add_argument("--add-room", action="append", default=[], type=_parse_add_room,
                    metavar="ID:BUILDING:NUMBER:CAPACITY:TYPE",
                    help='Repeatable, e.g. "SCI-301:Science Building:301:40:LECTURE"')
    ap.add_argument("--remove-room", action="append", default=[], metavar="ID",
                    help="Repeatable room id to remove from the scenario copy")
    ap.add_argument("--set-cap", action="append", default=[], type=_parse_set_cap,
                    metavar="COURSE:CAP",
                    help='Repeatable, e.g. "ENGL101:20"')
    ap.add_argument("--report", default=None,
                    help="Markdown report path (default exports/whatif_<semester>.md)")
    args = ap.parse_args()

    semester = args.semester.strip().upper()
    tweaks = ScenarioTweaks(
        add_faculty=args.add_faculty,
        remove_faculty=args.remove_faculty,
        add_rooms=args.add_room,
        remove_rooms=args.remove_room,
        set_max_class_size=dict(args.set_cap),
    )
    report = Path(args.report) if args.report else (
        Path("exports") / f"whatif_{semester}.md"
    )

    result = run_scenario(
        DEFAULT_DB_PATH,
        semester=semester,
        tweaks=tweaks,
        time_limit=args.time_limit,
        emit=print,
    )
    write_scenario_report(result, report)

    print()
    print("=" * 72)
    print(f"WHAT-IF SCENARIO: {semester}")
    print("=" * 72)
    print(f"{'Metric':<32}{'Baseline':>14}{'Scenario':>14}{'Delta':>12}")
    print("-" * 72)
    for label, key in METRIC_ROWS:
        b = format_metric_value(key, result.baseline_metrics.get(key))
        s = format_metric_value(key, result.scenario_metrics.get(key))
        d = format_metric_delta(key, result.deltas.get(key))
        print(f"{label:<32}{b:>14}{s:>14}{d:>12}")
    print()
    print(f"Report written to {report}")

    ok = ("optimal", "feasible")
    if result.baseline_status not in ok or result.scenario_status not in ok:
        print(
            f"WARNING: baseline={result.baseline_status}, "
            f"scenario={result.scenario_status} — see report for staffing warnings."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
