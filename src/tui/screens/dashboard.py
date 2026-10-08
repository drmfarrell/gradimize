"""Dashboard screen for the TUI."""

import logging

from sqlmodel import Session, func, select
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static

from src.database.connection import get_engine
from src.database.models import (
    Course,
    Faculty,
    Room,
    ScheduleVersion,
    Student,
    StudentDemand,
    TimeSlot,
)
from src.tui.widgets.solver_viz import SolverVizPanel

logger = logging.getLogger(__name__)


def _count(session: Session, model) -> int:
    return session.exec(select(func.count()).select_from(model)).one()

class DashboardScreen(Screen):
    """Main dashboard screen."""

    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("s", "switch_mode('schedule')", "Schedule"),
        ("c", "switch_mode('conflicts')", "Conflicts"),
        ("b", "switch_mode('browser')", "Data Browser"),
        ("f", "switch_mode('finals')", "Finals"),
        ("w", "switch_mode('settings')", "Settings"),
        ("m", "switch_mode('import')", "Import"),
        ("g", "generate_schedule", "Generate"),
        ("h", "health_check", "Health"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    DashboardScreen {
        layout: vertical;
    }

    #dashboard-container {
        height: 100%;
        width: 100%;
    }

    #top-row {
        height: auto;
        max-height: 40%;
        width: 100%;
    }

    #bottom-row {
        height: 1fr;
        width: 100%;
    }

    .panel {
        width: 1fr;
        height: 100%;
        margin: 1;
        padding: 1;
        border: solid green;
    }

    #stats-panel {
        width: 1fr;
    }

    #schedules-panel {
        width: 1fr;
    }

    #actions-panel {
        width: 1fr;
        max-width: 35;
    }

    #log-panel {
        width: 1fr;
        height: 100%;
        margin: 1;
        padding: 0;
        border: solid green;
    }

    #solver-log {
        height: 1fr;
        scrollbar-size: 1 1;
    }

    #log-title {
        height: 1;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the dashboard."""
        yield Header()
        yield Container(
            Horizontal(
                Vertical(
                    Static("[bold]System Statistics[/bold]"),
                    Static(id="stats-content"),
                    id="stats-panel",
                    classes="panel",
                ),
                Vertical(
                    Static("[bold]Recent Schedules[/bold]"),
                    Static(id="schedules-content"),
                    id="schedules-panel",
                    classes="panel",
                ),
                id="top-row",
            ),
            Horizontal(
                Vertical(
                    Static("[bold]Quick Actions[/bold]"),
                    Static("\\[G] Generate new schedule"),
                    Static("\\[M] Import data files"),
                    Static("\\[S] View schedules"),
                    Static("\\[C] View conflicts"),
                    Static("\\[B] Browse data"),
                    Static("\\[F] Finals"),
                    Static("\\[W] Settings"),
                    Static("\\[Q] Quit"),
                    id="actions-panel",
                    classes="panel",
                ),
                Vertical(
                    Static("[bold]Solver Log[/bold]", id="log-title"),
                    SolverVizPanel(id="solver-viz"),
                    RichLog(highlight=True, markup=True, id="solver-log"),
                    id="log-panel",
                ),
                id="bottom-row",
            ),
            id="dashboard-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Called when screen is mounted."""
        self.load_stats()
        self.load_schedules()
        log = self.query_one("#solver-log", RichLog)
        log.write("[dim]Ready. Press \\[G] to generate a schedule.[/dim]")

    def on_screen_resume(self) -> None:
        """Refresh panels and start any solve handed off from another screen.

        The schedule screen's re-solve/repair actions acquire the job lock,
        set app.pending_job, and switch here — so those runs get the
        dashboard's log + live viz instead of a duplicated pair of widgets.
        The worker releases the lock.
        """
        self.load_stats()
        self.load_schedules()
        job = getattr(self.app, "pending_job", None)
        if not job:
            return
        self.app.pending_job = None
        kind, schedule_id = job
        log = self.query_one("#solver-log", RichLog)
        log.clear()
        label = "re-solve" if kind == "resolve" else "repair"
        log.write(f"[yellow bold]Starting {label} of schedule "
                  f"{schedule_id}...[/yellow bold]")
        self._viz_panel = self.query_one("#solver-viz", SolverVizPanel)
        self._viz_panel.display = False
        if kind == "resolve":
            self.run_worker(lambda: self._do_resolve(schedule_id), thread=True)
        else:
            self.run_worker(lambda: self._do_repair(schedule_id), thread=True)

    def load_stats(self) -> None:
        """Load and display system statistics."""
        engine = get_engine()
        with Session(engine) as session:
            stats = {
                "Students": _count(session, Student),
                "Courses": _count(session, Course),
                "Faculty": _count(session, Faculty),
                "Rooms": _count(session, Room),
                "Time Slots": _count(session, TimeSlot),
                "Demands": _count(session, StudentDemand),
            }

        content = self.query_one("#stats-content", Static)
        lines = [f"  {k}: [cyan]{v}[/cyan]" for k, v in stats.items()]
        content.update("\n".join(lines))

    def load_schedules(self) -> None:
        """Load and display recent schedules."""
        engine = get_engine()
        with Session(engine) as session:
            schedules = list(
                session.exec(
                    select(ScheduleVersion).order_by(
                        ScheduleVersion.created_at.desc()  # type: ignore
                    ).limit(5)
                ).all()
            )

        content = self.query_one("#schedules-content", Static)
        if schedules:
            lines = []
            for s in schedules:
                status_color = {"draft": "yellow", "approved": "green", "archived": "dim"}.get(
                    s.status, "white"
                )
                lines.append(
                    f"  [{status_color}]{s.name}[/{status_color}] ({s.semester}) - {s.status}"
                )
            content.update("\n".join(lines))
        else:
            content.update("  No schedules found.\n  Press \\[G] to generate one.")

    def _write_log(self, message: str) -> None:
        """Write a message to the solver log (thread-safe)."""
        self.app.call_from_thread(self._append_log, message)

    def _append_log(self, message: str) -> None:
        """Append a message to the solver log (must be called from main thread)."""
        try:
            log = self.query_one("#solver-log", RichLog)
            log.write(message)
        except Exception:
            pass

    def action_switch_mode(self, mode: str) -> None:
        """Switch to a different screen."""
        self.app.switch_mode(mode)

    def action_generate_schedule(self) -> None:
        """Generate a new schedule in a background thread."""
        from src.core import job_lock

        log = self.query_one("#solver-log", RichLog)
        # Same single-optimization guard as the web API — pressing G twice
        # (or solving while the web UI solves) must not race two CP-SAT runs
        # against one SQLite file.
        if not job_lock.try_acquire("generate", "TUI Generated"):
            log.write(
                "[yellow]An optimization is already running — wait for it to "
                "finish before starting another.[/yellow]"
            )
            return
        log.clear()
        log.write("[yellow bold]Starting schedule generation...[/yellow bold]")
        # Fetched on the main thread; the worker thread only reads this
        # reference (SolverVizPanel.handle_event marshals its own updates).
        self._viz_panel = self.query_one("#solver-viz", SolverVizPanel)
        self._viz_panel.display = False
        self.run_worker(self._do_generate_schedule, thread=True)


    def _make_emit(self):
        """Emit handler shared by generate/re-solve/repair workers: log
        lines to the RichLog, terminal events to a summary line, everything
        else to the live viz panel."""
        def emit(event_type: str, **fields) -> None:
            if event_type == "log":
                self._write_log(fields.get("msg", ""))
            elif event_type == "done":
                self._write_log(
                    f"[green bold]Done! Status: {fields.get('status')}, "
                    f"{fields.get('sections_count')} sections, "
                    f"{fields.get('enrollments_count')} enrollments, "
                    f"objective: {fields.get('objective_value', 0):.0f}, "
                    f"time: {fields.get('solve_time_seconds', 0):.1f}s[/green bold]"
                )
                self.app.call_from_thread(self.load_schedules)
            elif event_type == "error":
                self._write_log(f"[red bold]Failed: {fields.get('message')}[/red bold]")
            else:
                panel = getattr(self, "_viz_panel", None)
                if panel is not None:
                    panel.handle_event(event_type, fields)

        return emit

    def _do_generate_schedule(self) -> None:
        """Run the shared generation pipeline (worker thread).

        Delegates to core.generate.run_generation — the same code path the
        web UI uses — instead of a TUI-local copy that had drifted (it
        counted stale projected demands when deciding whether to
        recalculate). job_lock is acquired by action_generate_schedule and
        released here.
        """
        from src.core import job_lock
        from src.core.generate import run_generation

        emit = self._make_emit()
        try:
            run_generation(
                emit,
                semester=self.app.semester,
                name="TUI Generated",
                workers=self.app.solver_workers,
                time_limit=self.app.solver_time,
                include_projections=self.app.include_projections,
                class_size_cap=self.app.class_size_cap or None,
                capacity_mode=self.app.capacity_mode or None,
                conflict_threshold=(
                    self.app.conflict_threshold
                    if self.app.conflict_threshold >= 0 else None
                ),
            )
        finally:
            job_lock.release()

    def _do_resolve(self, schedule_id: int) -> None:
        """Re-solve keeping pinned sections fixed (worker thread). The
        schedule screen acquired the job lock before handing off."""
        from src.core import job_lock
        from src.core.generate import run_resolve

        try:
            run_resolve(
                self._make_emit(), schedule_id,
                workers=self.app.solver_workers,
                time_limit=self.app.solver_time,
            )
        finally:
            job_lock.release()

    def _do_repair(self, schedule_id: int) -> None:
        """Minimal-change repair against current data (worker thread). The
        schedule screen acquired the job lock before handing off. MOVED /
        DROPPED lines arrive as ordinary log events from run_repair."""
        from src.core import job_lock
        from src.core.generate import run_repair

        try:
            run_repair(
                self._make_emit(), schedule_id,
                workers=self.app.solver_workers,
                time_limit=self.app.solver_time,
            )
        finally:
            job_lock.release()

    def action_health_check(self) -> None:
        """Run the data-health doctor in a background thread."""
        log = self.query_one("#solver-log", RichLog)
        log.clear()
        log.write("[yellow bold]Running data-health checks...[/yellow bold]")
        self.run_worker(self._do_health_check, thread=True)

    def _do_health_check(self) -> None:
        from sqlmodel import Session

        from src.core.doctor import overall_status, run_health_checks
        from src.database.connection import get_engine

        styles = {"ok": "green", "warn": "yellow", "fail": "red"}
        try:
            with Session(get_engine()) as session:
                results = run_health_checks(session)
        except Exception as e:
            logger.exception("Health check failed")
            self._write_log(f"[red bold]Health check failed: {e}[/red bold]")
            return
        for r in results:
            style = styles[r.status]
            self._write_log(
                f"[{style}]{r.status.upper():>4}[/{style}] {r.name}: {r.summary}"
            )
            for d in r.details:
                self._write_log(f"      - {d}")
            if r.count > len(r.details):
                self._write_log(f"      … and {r.count - len(r.details)} more")
        status = overall_status(results)
        style = styles[status]
        self._write_log(
            f"[{style} bold]Overall: {status.upper()}[/{style} bold]"
            + (" — fix every FAIL before generating." if status == "fail" else "")
        )

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()
