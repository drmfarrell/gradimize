#!/usr/bin/env python3
"""Derive Faculty.approved_courses/preferences from historical Banner extracts.

Usage:
    python scripts/derive_teaching_history.py DIR [--overwrite] [--dry-run]

DIR contains one or more schedule_*.csv files in the data-request layout
(see exports/Gradimize_Data_Request.md §6/§7 and
exports/data_request_examples/schedule_202610.csv). Fill-only by default
(only touches faculty with an empty approved_courses list / a missing
preference key); --overwrite replaces both regardless. --dry-run prints
the per-faculty before->after report without committing. Respects
GRADIMIZE_DATA_DIR like every other entry point.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session

from src.core.teaching_history import derive_teaching_history
from src.database.connection import create_tables, get_engine


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Derive faculty teaching history from Banner schedule extracts"
    )
    ap.add_argument("directory", help="Directory containing schedule_*.csv files")
    ap.add_argument(
        "--overwrite", action="store_true",
        help="Replace approved_courses/preferred_slots instead of filling only empty/absent",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print the report without committing any changes",
    )
    args = ap.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Not a directory: {directory}", file=sys.stderr)
        return 2

    engine = get_engine()
    create_tables(engine)
    with Session(engine) as session:
        stats = derive_teaching_history(
            session,
            directory,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            emit=print,
        )

    print()
    print("=" * 72)
    print("TEACHING HISTORY DERIVATION")
    print("=" * 72)
    print(f"Files scanned:            {stats['files']}")
    print(f"Rows parsed:               {stats['rows']}")
    print(f"Rows skipped (no instructor): {stats['rows_skipped_no_instructor']}")
    print(f"Faculty updated:           {stats['faculty_updated']}")
    print(f"Faculty skipped (already curated, not --overwrite): "
          f"{stats['faculty_skipped_nonempty']}")
    if stats["unknown_instructor_ids"]:
        print(f"Unknown instructor IDs ({len(stats['unknown_instructor_ids'])}): "
              f"{', '.join(stats['unknown_instructor_ids'])}")
    if stats["dry_run"]:
        print("\nDRY RUN -- nothing was committed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
