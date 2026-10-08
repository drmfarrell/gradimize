#!/usr/bin/env python3
"""Policy profiles: solve the same term under named weight bundles, side by side.

Usage:
    python scripts/compare_profiles.py --semester 2026FA \
        [--profiles balanced,students_first,faculty_first,efficiency_first] \
        [--time-limit 120] [--report exports/profiles_2026FA.md]

Runs one full solver pass PER PROFILE against throwaway copies of the live
database (GRADIMIZE_DATA_DIR respected) — the live data is never modified.
Budget roughly (number of profiles) x --time-limit seconds of wall time.
The output is a Pareto trade-off table: what each policy buys, and from whom.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.profiles import (
    PROFILES,
    compare_profiles,
    render_profiles_markdown,
)
from src.database.connection import DEFAULT_DB_PATH


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Solve one term under multiple policy profiles and compare."
    )
    ap.add_argument("--semester", required=True, help="Semester to solve, e.g. 2026FA")
    ap.add_argument(
        "--profiles",
        default=",".join(PROFILES),
        help=f"Comma-separated profile names (default: all — {', '.join(PROFILES)})",
    )
    ap.add_argument("--time-limit", type=int, default=120,
                    help="Solver seconds per profile (default 120)")
    ap.add_argument("--report", default=None,
                    help="Markdown report path (default exports/profiles_<SEM>.md)")
    args = ap.parse_args()

    semester = args.semester.strip().upper()
    names = [p.strip() for p in args.profiles.split(",") if p.strip()]
    report = Path(args.report) if args.report else (
        Path("exports") / f"profiles_{semester}.md"
    )

    result = compare_profiles(
        DEFAULT_DB_PATH,
        semester=semester,
        profile_names=names,
        time_limit=args.time_limit,
        emit=print,
    )
    md = render_profiles_markdown(result)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(md, encoding="utf-8")

    print()
    print(md)
    print(f"Report written to {report}")

    ok = ("optimal", "feasible")
    bad = [p["name"] for p in result["profiles"] if p["status"] not in ok]
    if bad:
        print(f"WARNING: profiles {', '.join(bad)} did not solve — see report.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
