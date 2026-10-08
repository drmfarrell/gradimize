#!/usr/bin/env python3
"""Absorption test CLI: see src/core/absorption.py.

Usage:
    python scripts/absorption_test.py --schedule-id 1 --schedule-id 2 \
        [--top-unmet 10] [--report-md exports/absorption_2026FA.md]

Read-only: seats the CURRENT StudentDemand rows into each named schedule's
EXISTING, FIXED sections and reports how much demand each timetable
absorbs. Nothing in the database is modified. Prints a side-by-side table
to stdout; writes a markdown report too when --report-md is given.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session

from src.core.absorption import (
    compare_absorption,
    render_absorption_markdown,
    render_absorption_text,
)
from src.database.connection import DEFAULT_DB_PATH, get_engine


def main() -> int:
    ap = argparse.ArgumentParser(description="Gradimize absorption test (experiment B)")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH),
                    help="Database path (default: the live DB); read-only")
    ap.add_argument("--schedule-id", type=int, action="append", required=True,
                    dest="schedule_ids",
                    help="Schedule ID to score; repeat for side-by-side comparison")
    ap.add_argument("--top-unmet", type=int, default=10,
                    help="How many highest-unmet courses to list per schedule (default 10)")
    ap.add_argument("--report-md", default=None,
                    help="Optional markdown report file path (always printed to stdout too)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    engine = get_engine(db_path)
    with Session(engine) as session:
        try:
            rows = compare_absorption(session, args.schedule_ids, top_unmet=args.top_unmet)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(render_absorption_text(rows))

    if args.report_md:
        md = render_absorption_markdown(rows)
        report_path = Path(args.report_md)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")
        print(f"Report written to {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
