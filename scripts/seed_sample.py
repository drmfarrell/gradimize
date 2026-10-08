#!/usr/bin/env python3
"""Seed the database from sample (or other) CSV/YAML data.

Usage:
    python scripts/seed_sample.py [--data-dir data/sample] [--time-slots config/time_slots.yaml]

This is the seeding logic factored out of serve.sh's seed_data().
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session

from src.core.demand import DemandCalculator
from src.core.graph import PrerequisiteGraph
from src.core.ingest import ingest_data
from src.database.connection import create_tables, get_engine


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed Gradimize data")
    ap.add_argument("--data-dir", default="data/sample",
                    help="Directory of CSV data to ingest (default: data/sample)")
    ap.add_argument("--time-slots", default="config/time_slots.yaml",
                    help="Time slots config file (default: config/time_slots.yaml)")
    args = ap.parse_args()

    engine = get_engine()
    create_tables(engine)
    with Session(engine) as s:
        for k, v in ingest_data(s, args.data_dir, args.time_slots).items():
            print(f'  {k}: {v}')
        s.commit()
        # calculate_all_demands PERSISTS demand rows (calculate_student_demand
        # only returns them, which is how an earlier version seeded 0 demands)
        n = DemandCalculator(s, PrerequisiteGraph(s)).calculate_all_demands()
        print(f'  demands: {n}')
    print('Data loaded.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
