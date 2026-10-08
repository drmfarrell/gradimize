#!/usr/bin/env python3
"""Project schedules for the next N regular terms (see src/core/longrange.py).

Usage:
    python scripts/project_terms.py --start 2026FA --terms 6 \
        [--time-limit 180] [--report exports/projection.md]

Runs against a scratch COPY of the live database (GRADIMIZE_DATA_DIR
respected) — the live data is never modified. Each term is a full solver
run, so budget roughly --time-limit seconds per term of wall time.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.longrange import project_terms, write_projection_report
from src.database.connection import DEFAULT_DB_PATH


def main() -> int:
    ap = argparse.ArgumentParser(description="Gradimize multi-semester projection")
    ap.add_argument("--start", required=True, help="First term, e.g. 2026FA")
    ap.add_argument("--terms", type=int, default=6,
                    help="Number of regular terms to simulate (default 6 = 3 years)")
    ap.add_argument("--time-limit", type=int, default=180,
                    help="Solver seconds per term (default 180)")
    ap.add_argument("--report", default=None,
                    help="Markdown report path (default exports/projection_<start>_<terms>t.md)")
    ap.add_argument("--grad-credits", type=int, default=120,
                    help="Credits needed to graduate out of the simulation")
    args = ap.parse_args()

    report = Path(args.report) if args.report else (
        Path("exports") / f"projection_{args.start}_{args.terms}t.md"
    )

    results, scratch = project_terms(
        DEFAULT_DB_PATH,
        start_term=args.start.strip().upper(),
        n_terms=args.terms,
        time_limit_per_term=args.time_limit,
        grad_credits=args.grad_credits,
        emit=print,
    )
    write_projection_report(results, report, source_label=str(DEFAULT_DB_PATH))
    print(f"\nReport written to {report}")
    print(f"Scratch DB with per-term schedules kept at {scratch}")
    solved = sum(1 for r in results if r.status in ("optimal", "feasible"))
    print(f"{solved}/{len(results)} terms solved.")
    return 0 if solved == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
