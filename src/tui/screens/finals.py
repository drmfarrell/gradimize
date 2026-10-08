"""Finals exam scheduling screen for the TUI."""

import logging
from pathlib import Path

from sqlmodel import Session, select
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from src.core.finals import (
    DAY_LABELS,
    FinalsConfig,
    FinalsEngine,
    FinalsExporter,
)
from src.database.connection import get_engine
from src.database.models import (
    FinalExamAssignment,
    FinalExamBlock,
    FinalsSchedule,
    Room,
    ScheduleVersion,
    Section,
    TimeSlot,
)

# Anchored to the repo root — a CWD-relative path scattered export
# files wherever the TUI happened to be launched from.
EXPORT_DIR = Path(__file__).resolve().parents[3] / "exports"

logger = logging.getLogger(__name__)


class FinalsScreen(Screen):
    """Finals exam scheduling screen."""

    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("s", "switch_mode('schedule')", "Schedule"),
        ("c", "switch_mode('conflicts')", "Conflicts"),
        ("b", "switch_mode('browser')", "Data Browser"),
        ("w", "switch_mode('settings')", "Settings"),
        ("m", "switch_mode('import')", "Import"),
        ("n", "next_finals", "Next"),
        ("p", "prev_finals", "Previous"),
        ("g", "solve_finals", "Solve Finals"),
        ("e", "export_csv", "Export CSV"),
        ("x", "export_excel", "Export Excel"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    FinalsScreen {
        layout: vertical;
    }

    #finals-container {
        height: 100%;
        width: 100%;
        padding: 1;
    }

    #finals-header {
        height: 3;
        width: 100%;
        padding: 0 1;
    }

    #finals-table {
        height: 1fr;
        width: 100%;
        border: solid magenta;
    }

    #finals-log {
        height: 8;
        width: 100%;
        border: solid green;
        margin-top: 1;
    }

    #finals-status {
        height: 1;
        width: 100%;
        padding: 0 1;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._finals_schedules: list[FinalsSchedule] = []
        self._current_index = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static(id="finals-header"),
            DataTable(id="finals-table"),
            RichLog(highlight=True, markup=True, id="finals-log"),
            Static(id="finals-status"),
            id="finals-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#finals-table", DataTable)
        table.add_columns(
            "CRN", "Course", "Section", "Day", "Block", "Room", "In-Place"
        )
        self._load_finals_schedules()
        self._show_current()
        self.query_one("#finals-log", RichLog).write(
            "[dim]Press \\[G] to solve finals for the latest semester schedule. "
            "\\[N]/\\[P] cycle through finals schedules.[/dim]"
        )

    def on_screen_resume(self) -> None:
        """Refresh when returning to this screen (MODES screens mount once)."""
        self._load_finals_schedules()
        self._show_current()

    def _load_finals_schedules(self) -> None:
        engine = get_engine()
        with Session(engine) as session:
            self._finals_schedules = list(
                session.exec(
                    select(FinalsSchedule).order_by(
                        FinalsSchedule.created_at.desc()  # type: ignore
                    )
                ).all()
            )
        # Keep the index valid if the list shrank while suspended.
        self._current_index = min(
            self._current_index, max(0, len(self._finals_schedules) - 1)
        )

    def _show_current(self) -> None:
        header = self.query_one("#finals-header", Static)
        table = self.query_one("#finals-table", DataTable)
        table.clear()

        if not self._finals_schedules:
            header.update("[bold]No finals schedules yet[/bold] — press [G] to solve.")
            return

        fs = self._finals_schedules[self._current_index]
        stats = fs.solver_stats or {}
        header.update(
            f"[bold]{fs.name}[/bold] (schedule_id={fs.schedule_id}) — "
            f"{fs.status.upper()} — "
            f"obj={stats.get('objective', 0):.0f}, "
            f"time={stats.get('solve_time_seconds', 0):.1f}s, "
            f"sections={stats.get('num_solver_sections', 0)} solver / "
            f"{stats.get('num_in_place_sections', 0)} in-place / "
            f"{stats.get('num_skipped_sections', 0)} skipped — "
            f"[{self._current_index + 1}/{len(self._finals_schedules)}]"
        )

        engine = get_engine()
        with Session(engine) as session:
            blocks = list(
                session.exec(
                    select(FinalExamBlock).where(
                        FinalExamBlock.finals_schedule_id == fs.id
                    )
                ).all()
            )
            block_by_id = {b.id: b for b in blocks}
            assignments = list(
                session.exec(
                    select(FinalExamAssignment).where(
                        FinalExamAssignment.finals_schedule_id == fs.id
                    )
                ).all()
            )
            sec_ids = [a.section_id for a in assignments]
            sections = (
                list(
                    session.exec(
                        select(Section).where(Section.id.in_(sec_ids))
                    ).all()
                )
                if sec_ids
                else []
            )
            sec_by_id = {s.id: s for s in sections}
            rooms = {r.id: r for r in session.exec(select(Room)).all()}
            time_slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}

            rows = []
            for a in assignments:
                sec = sec_by_id.get(a.section_id)
                if not sec:
                    continue
                room = rooms.get(a.room_id)
                room_label = (
                    f"{room.building} {room.number}" if room else a.room_id
                )
                if a.is_in_place and a.in_place_time_slot_id is not None:
                    ts = time_slots.get(a.in_place_time_slot_id)
                    day = ",".join(ts.days) if ts else "-"
                    block_label = (
                        f"{ts.start_time}-{ts.end_time}" if ts else "in-place"
                    )
                else:
                    blk = block_by_id.get(a.block_id)
                    day = (
                        DAY_LABELS.get(blk.day, blk.day) if blk else "-"
                    )
                    block_label = (
                        f"{blk.start_time}-{blk.end_time}" if blk else "-"
                    )
                rows.append((
                    sec.crn,
                    sec.course_code,
                    sec.section_number,
                    day,
                    block_label,
                    room_label,
                    "Y" if a.is_in_place else "",
                ))

            rows.sort(key=lambda r: (r[3], r[4], r[1], r[2]))
            for r in rows:
                table.add_row(*r)

    def _set_status(self, message: str) -> None:
        self.query_one("#finals-status", Static).update(message)

    def _write_log(self, message: str) -> None:
        try:
            self.query_one("#finals-log", RichLog).write(message)
        except Exception:
            pass

    def action_next_finals(self) -> None:
        if self._finals_schedules and self._current_index < len(
            self._finals_schedules
        ) - 1:
            self._current_index += 1
            self._show_current()

    def action_prev_finals(self) -> None:
        if self._finals_schedules and self._current_index > 0:
            self._current_index -= 1
            self._show_current()

    def _latest_schedule_id(self) -> int | None:
        engine = get_engine()
        with Session(engine) as session:
            sv = session.exec(
                select(ScheduleVersion).order_by(
                    ScheduleVersion.created_at.desc()  # type: ignore
                ).limit(1)
            ).first()
            return sv.id if sv else None

    def action_solve_finals(self) -> None:
        from src.core import job_lock

        log = self.query_one("#finals-log", RichLog)
        # Same single-optimization guard as the web API — no concurrent
        # CP-SAT runs against one SQLite file (see dashboard generate).
        if not job_lock.try_acquire("finals", "TUI Finals"):
            log.write(
                "[yellow]An optimization is already running — wait for it to "
                "finish before starting another.[/yellow]"
            )
            return
        log.clear()
        log.write("[yellow bold]Solving finals...[/yellow bold]")
        self.run_worker(self._do_solve_finals, thread=True)

    def _do_solve_finals(self) -> None:
        from src.core import job_lock

        try:
            schedule_id = self._latest_schedule_id()
            if schedule_id is None:
                self.app.call_from_thread(
                    self._write_log, "[red]No semester schedule found.[/red]"
                )
                return
            self.app.call_from_thread(
                self._write_log,
                f"[dim]Target semester schedule: {schedule_id}[/dim]",
            )
            engine = get_engine()
            with Session(engine) as session:
                cfg = FinalsConfig(
                    max_time_seconds=self.app.solver_time,
                    num_workers=self.app.solver_workers,
                )
                self.app.call_from_thread(
                    self._write_log,
                    f"[dim]Config: start={cfg.start_time}, "
                    f"blocks/day={cfg.blocks_per_day}, "
                    f"days={','.join(cfg.exam_days)}, "
                    f"overflow={','.join(cfg.overflow_days)}, "
                    f"evening_threshold={cfg.evening_in_place_threshold}[/dim]",
                )
                fe = FinalsEngine(session, schedule_id, cfg)
                self.app.call_from_thread(
                    self._write_log,
                    f"[dim]Sections: {len(fe.solver_sections)} solver / "
                    f"{len(fe.in_place_sections)} in-place / "
                    f"{len(fe.skipped_section_ids)} skipped[/dim]",
                )
                result = fe.solve(name="TUI Finals")

            if result.status in ("optimal", "feasible"):
                self.app.call_from_thread(
                    self._write_log,
                    f"[green bold]{result.status}[/green bold] — "
                    f"obj={result.objective_value:.0f}, "
                    f"time={result.solve_time_seconds:.2f}s, "
                    f"{len(result.assignments)} assignments",
                )
                self.app.call_from_thread(self._after_solve)
            else:
                self.app.call_from_thread(
                    self._write_log,
                    f"[red bold]Failed: {result.status}[/red bold]",
                )
        except Exception as e:
            logger.exception("Finals solve failed")
            self.app.call_from_thread(
                self._write_log, f"[red bold]Error: {e}[/red bold]"
            )
        finally:
            job_lock.release()

    def _after_solve(self) -> None:
        self._load_finals_schedules()
        self._current_index = 0
        self._show_current()

    def _current_finals_id(self) -> int | None:
        if not self._finals_schedules:
            return None
        return self._finals_schedules[self._current_index].id

    def action_export_csv(self) -> None:
        fid = self._current_finals_id()
        if fid is None:
            self._set_status("[red]No finals schedule to export[/red]")
            return
        try:
            engine = get_engine()
            with Session(engine) as session:
                out = EXPORT_DIR / f"finals_{fid}.csv"
                FinalsExporter(session).export_csv(fid, out)
            self._set_status(f"[green]Wrote {out}[/green]")
        except Exception as e:
            logger.exception("Export failed")
            self._set_status(f"[red]Export failed: {e}[/red]")

    def action_export_excel(self) -> None:
        fid = self._current_finals_id()
        if fid is None:
            self._set_status("[red]No finals schedule to export[/red]")
            return
        try:
            engine = get_engine()
            with Session(engine) as session:
                out = EXPORT_DIR / f"finals_{fid}.xlsx"
                FinalsExporter(session).export_excel(fid, out)
            self._set_status(f"[green]Wrote {out}[/green]")
        except Exception as e:
            logger.exception("Export failed")
            self._set_status(f"[red]Export failed: {e}[/red]")

    def action_switch_mode(self, mode: str) -> None:
        self.app.switch_mode(mode)

    def action_quit(self) -> None:
        self.app.exit()
