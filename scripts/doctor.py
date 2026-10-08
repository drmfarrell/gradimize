#!/usr/bin/env python3
"""Gradimize data-health doctor — vet the database before solving.

Usage:
    python scripts/doctor.py [--skip-solver-checks] [--json]

Run it right after importing a data delivery. Exit codes: 0 = all ok,
1 = warnings only, 2 = at least one FAIL (fix before generating schedules).
Respects GRADIMIZE_DATA_DIR like every other entry point.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session

from src.core.doctor import overall_status, run_health_checks
from src.database.connection import create_tables, get_engine

ICONS = {"ok": "  OK ", "warn": "WARN ", "fail": "FAIL "}
COLORS = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m"}
RESET = "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser(description="Gradimize data-health checks")
    ap.add_argument("--skip-solver-checks", action="store_true",
                    help="Skip the (slower) staffing pre-flight")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    ap.add_argument("--show-values", action="store_true",
                    help="Print student-level examples (ids, grades, per-student "
                         "attributes). Default output redacts them to counts, so "
                         "it is safe to paste into email or an AI assistant.")
    args = ap.parse_args()

    from src.core.runlog import RunLog

    engine = get_engine()
    create_tables(engine)
    rl = RunLog("doctor")
    try:
        with Session(engine) as session:
            results = run_health_checks(
                session, deep=not args.skip_solver_checks, runlog=rl)
    finally:
        rl.close()

    status = overall_status(results)
    if args.json:
        out = []
        for r in results:
            d = r.as_dict()
            if r.protected and not args.show_values:
                d["details"] = []
                d["details_redacted"] = True
            out.append(d)
        print(json.dumps({"status": status, "results": out}, indent=2))
    else:
        use_color = sys.stdout.isatty()
        for r in results:
            prefix = ICONS[r.status]
            if use_color:
                prefix = f"{COLORS[r.status]}{prefix}{RESET}"
            print(f"{prefix} {r.name}: {r.summary}")
            if r.protected and not args.show_values:
                if r.details:
                    print(f"        - [{r.count} student-level example(s) "
                          f"redacted — rerun with --show-values to see them]")
            else:
                for d in r.details:
                    print(f"        - {d}")
                if r.count > len(r.details):
                    print(f"        … and {r.count - len(r.details)} more")
        n_fail = sum(1 for r in results if r.status == "fail")
        n_warn = sum(1 for r in results if r.status == "warn")
        print(f"\nOverall: {status.upper()} "
              f"({n_fail} fail, {n_warn} warn, "
              f"{len(results) - n_fail - n_warn} ok)")
        if n_fail:
            print("Fix every FAIL before generating schedules.")
        if rl.path:
            print(f"Run log (student-data-free): {rl.path}")

    return {"ok": 0, "warn": 1, "fail": 2}[status]


if __name__ == "__main__":
    sys.exit(main())
