#!/usr/bin/env python3
"""Bulk student-schedule export CLI: see src/core/distribution.py.

Usage:
    python scripts/export_student_schedules.py --schedule-id 1 --out exports/dist

Read-only: writes student_schedules.csv, sheets/<student_id>.md, and
ical/<student_id>.ics under --out. Output is keyed by pseudonym; use the
registrar-side reidentify.py tool to swap pseudonyms back to real IDs
before distributing.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session

from src.core.distribution import export_student_schedules
from src.database.connection import DEFAULT_DB_PATH, get_engine


def main() -> int:
    ap = argparse.ArgumentParser(description="Gradimize bulk student-schedule export")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH),
                    help="Database path (default: the live DB); read-only")
    ap.add_argument("--schedule-id", type=int, required=True,
                    help="Schedule ID to export")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--no-sheets", action="store_true",
                    help="Skip per-student advisor sheets")
    ap.add_argument("--no-ical", action="store_true",
                    help="Skip per-student .ics calendars")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    engine = get_engine(db_path)
    with Session(engine) as session:
        try:
            stats = export_student_schedules(
                session, args.schedule_id, args.out,
                include_sheets=not args.no_sheets,
                include_ical=not args.no_ical,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(f"Schedule {args.schedule_id} -> {args.out}")
    print(f"  Students:                  {stats['students']}")
    print(f"  Enrollments:               {stats['enrollments']}")
    print(f"  CSV rows:                  {stats['csv_rows']}")
    print(f"  Sheets written:            {stats['sheets_written']}")
    print(f"  ICS files written:         {stats['ics_written']}")
    print(f"  Sections without meetings: {stats['sections_without_meetings']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
