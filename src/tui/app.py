"""Main TUI application for Gradimize."""

import argparse
import os
import sys

os.environ["NO_COLOR"] = "1"

from textual.app import App

from src.database.connection import create_tables, get_engine
from src.tui.screens.constraints import ConflictsScreen
from src.tui.screens.dashboard import DashboardScreen
from src.tui.screens.data_browser import DataBrowserScreen
from src.tui.screens.finals import FinalsScreen
from src.tui.screens.import_screen import ImportScreen
from src.tui.screens.schedule import ScheduleScreen
from src.tui.screens.settings import SettingsScreen


class GradimizeApp(App):
    """Gradimize Terminal User Interface."""

    TITLE = "Gradimize - Academic Scheduling Engine"
    CSS = """
    Screen {
        background: $surface;
    }
    """

    MODES = {
        "dashboard": DashboardScreen,
        "schedule": ScheduleScreen,
        "conflicts": ConflictsScreen,
        "browser": DataBrowserScreen,
        "finals": FinalsScreen,
        "settings": SettingsScreen,
        "import": ImportScreen,
    }

    def __init__(self, solver_workers: int = 0, solver_time: int = 120,
                 semester: str = "2026FA", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.solver_workers = solver_workers
        self.solver_time = solver_time
        # Target semester for generated schedules (editable in Settings)
        self.semester = semester
        # Class-size knobs (editable in Settings; falsy = use YAML/config).
        # class_size_cap: hard seat cap per class for generated schedules.
        # capacity_mode: "fixed" | "room" ("" = config default) — "room"
        # lets class sizes float up to the assigned room's capacity.
        self.class_size_cap: int = 0
        self.capacity_mode: str = ""
        # Conflict tolerance: course pairs whose shared-student urgency sum
        # exceeds this get penalized for overlapping. -1 = config default.
        # Lower = stricter; small cohorts can afford stricter values.
        self.conflict_threshold: float = -1.0
        # Fold projected freshmen demand (active FreshmenProjection rows)
        # into Generate. Off by default, matching the web UI.
        self.include_projections: bool = False
        # Solve handed off between screens: the schedule screen acquires the
        # job lock, sets ("resolve"|"repair", schedule_id) here, and switches
        # to the dashboard, whose on_screen_resume starts the worker.
        self.pending_job: tuple[str, int] | None = None
        # Initialize database
        engine = get_engine()
        create_tables(engine)

    def on_mount(self) -> None:
        """Called when app is mounted."""
        self.switch_mode("dashboard")


def main():
    """Entry point for the TUI."""
    parser = argparse.ArgumentParser(description="Gradimize TUI")
    parser.add_argument(
        "--workers", type=int, default=0, help="Number of solver workers (0 = use all cores)"
    )
    parser.add_argument(
        "--solver-time",
        type=int,
        default=120,
        help="Solver time limit in seconds (default: 120)",
    )
    parser.add_argument(
        "--semester", type=str, default="2026FA", help="Semester code to schedule (e.g. 2026FA)"
    )
    parser.add_argument("--no-viz", action="store_true",
                        help="Disable the live solver visualization (on by default)")
    args = parser.parse_args()

    if args.no_viz:
        os.environ["GRADIMIZE_VIZ"] = "0"

    from src.core.logsetup import init_logging
    logfile = init_logging(app="tui")

    app = GradimizeApp(solver_workers=args.workers, solver_time=args.solver_time,
                       semester=args.semester)
    try:
        app.run()
    except Exception:
        # Textual restores the terminal before re-raising; make sure the
        # crash also lands in the log file the bug reporter collects.
        import logging
        logging.getLogger(__name__).exception("TUI crashed")
        print(f"Gradimize crashed — traceback saved to {logfile}\n"
              f"Run ./bugreport.sh to package it for support.",
              file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
