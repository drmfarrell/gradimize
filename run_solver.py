#!/usr/bin/env python3
"""CLI solver runner for development/debugging."""

import argparse
import logging
import sys

from sqlmodel import Session, select

from src.core.conflicts import build_conflict_matrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.core.generate import bootstrap_demands
from src.database.connection import create_tables, get_engine
from src.database.models import StudentDemand

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)




def main():
    parser = argparse.ArgumentParser(description="Gradimize CLI Solver")
    parser.add_argument(
        "--degreeworks", type=str, default=None,
        help="Path to DegreeWorks courses-needed CSV"
    )
    parser.add_argument(
        "--demand-source", choices=["degreeworks", "internal"], default=None,
        help="Demand source: 'degreeworks' (default) or 'internal'"
    )
    parser.add_argument(
        "--time-limit", type=int, default=120,
        help="Solver time limit in seconds (default: 120)"
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to scheduler YAML config"
    )
    parser.add_argument(
        "--semester", type=str, default="2026FA",
        help="Semester code to schedule (e.g. 2026FA)"
    )
    args = parser.parse_args()

    engine = get_engine()
    create_tables(engine)

    with Session(engine) as session:
        config = (
            SchedulerConfig(args.config) if args.config else SchedulerConfig.load_default()
        )
        config.solver_settings["max_time_seconds"] = args.time_limit
        config.solver_settings["num_workers"] = 0

        # CLI overrides
        if args.demand_source:
            config.demand_source = args.demand_source
        if args.degreeworks:
            config.demand_source = "degreeworks"
            config.degreeworks_csv = args.degreeworks

        demand_count = len(list(session.exec(select(StudentDemand)).all()))
        if demand_count == 0:
            bootstrap_demands(session, config, print)

        print("Building conflict matrix...")
        conflict_matrix = build_conflict_matrix(session)
        print("Done.")

        scheduler = SchedulingEngine(session, conflict_matrix, config)
        sections = scheduler.create_sections(args.semester)
        print(f"Sections to schedule: {len(sections)}")

        result = scheduler.solve(
            sections,
            "CLI Generated",
            args.semester,
            log_callback=lambda msg: print(msg, flush=True),
        )

        print()
        print(f"Status: {result.status}")
        print(f"Objective: {result.objective_value}")
        print(f"Sections: {len(result.sections)}")
        print(f"Enrollments: {len(result.enrollments)}")
        print(f"Solve time: {result.solve_time_seconds:.1f}s")


if __name__ == "__main__":
    main()
