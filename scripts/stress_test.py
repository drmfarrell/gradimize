#!/usr/bin/env python3
"""Enrollment stress test CLI: see src/core/stress.py.

Usage:
    python scripts/stress_test.py --semester 2026FA \
        [--factors 0.8,0.9,1.0,1.1,1.2] [--time-limit 60] \
        [--report-md exports/stress_2026FA.md]

Solves --semester once per scaling factor against THROWAWAY COPIES of the
source database (--db, default: the live DB via GRADIMIZE_DATA_DIR) — the
source is never modified. Budget roughly len(factors) * --time-limit
seconds of wall time. Always prints the markdown report to stdout; writes
it to a file too when --report-md is given. Exit code is 0 unless a hard
error occurs (a missing database, bad arguments, ...) — a factor whose
solve fails is a FINDING, reported in the output, not a script failure.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.stress import DEFAULT_FACTORS, render_stress_markdown, stress_test
from src.database.connection import DEFAULT_DB_PATH


def _parse_factors(spec: str) -> list[float]:
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("--factors must list at least one number")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--factors must be a comma-separated list of numbers, got {spec!r}"
        ) from None


def main() -> int:
    ap = argparse.ArgumentParser(description="Gradimize enrollment stress test")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH),
                    help="Source database path (default: the live DB)")
    ap.add_argument("--semester", required=True, help="Semester to solve, e.g. 2026FA")
    ap.add_argument("--factors", default=",".join(str(f) for f in DEFAULT_FACTORS),
                    type=_parse_factors,
                    help="Comma-separated incoming-class scaling factors "
                         "(default 0.8,0.9,1.0,1.1,1.2)")
    ap.add_argument("--time-limit", type=int, default=60,
                    help="Solver seconds PER FACTOR (default 60; solves len(factors) times)")
    ap.add_argument("--report-md", default=None,
                    help="Optional markdown report file path (always printed to stdout too)")
    args = ap.parse_args()

    semester = args.semester.strip().upper()

    result = stress_test(
        Path(args.db),
        semester=semester,
        factors=args.factors,
        time_limit=args.time_limit,
        log=print,
    )
    md = render_stress_markdown(result)

    print()
    print(md)

    if args.report_md:
        report_path = Path(args.report_md)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")
        print(f"Report written to {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
