"""Schedule viewer screen for the TUI."""

import logging
from pathlib import Path

from sqlmodel import Session, func, select
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from src.database.connection import get_engine
from src.database.models import (
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

# Anchored to the repo root — a CWD-relative path scattered export
# files wherever the TUI happened to be launched from.
EXPORT_DIR = Path(__file__).resolve().parents[3] / "exports"

logger = logging.getLogger(__name__)


class ScheduleScreen(Screen):
    """Schedule viewing screen."""

    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("c", "switch_mode('conflicts')", "Conflicts"),
        ("b", "switch_mode('browser')", "Data Browser"),
        ("f", "switch_mode('finals')", "Finals"),
        ("w", "switch_mode('settings')", "Settings"),
        ("n", "next_schedule", "Next"),
        ("p", "prev_schedule", "Previous"),
        ("e", "export_csv", "Export CSV"),
        ("x", "export_excel", "Export Excel"),
        ("a", "export_banner", "Banner Export"),
        ("i", "export_ical", "iCal"),
        ("v", "viability", "Viability"),
        ("u", "quality", "Quality"),
        ("r", "rebalance", "Rebalance"),
        ("t", "toggle_pin", "Pin/Unpin"),
        ("o", "resolve", "Re-solve"),
        ("k", "repair", "Repair"),
        ("y", "diff_prev", "Diff"),
        ("z", "check_conflicts", "Check"),
        ("j", "recommendations", "Suggest"),
        ("m", "switch_mode('import')", "Import"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    ScheduleScreen {
        layout: vertical;
    }

    #schedule-container {
        height: 100%;
        width: 100%;
        padding: 1;
    }

    #schedule-header {
        height: 3;
        width: 100%;
        padding: 0 1;
    }

    #schedule-table {
        height: 1fr;
        width: 100%;
        border: solid blue;
    }

    #export-status {
        /* auto: the viability/quality/diff/conflict/explain reports are
           multi-line — a fixed height of 1 clipped them to their first
           line. The table has 1fr and shrinks to make room; max-height
           keeps a huge report from squeezing it out entirely. */
        height: auto;
        max-height: 60%;
        width: 100%;
        padding: 0 1;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._schedules: list[ScheduleVersion] = []
        self._current_index = 0
        # Parallel to the table's rows: section id per row, for the
        # cursor-driven actions (pin toggle, Enter = explain).
        self._row_section_ids: list[int] = []

    def compose(self) -> ComposeResult:
        """Compose the screen."""
        yield Header()
        yield Container(
            Static(id="schedule-header"),
            DataTable(id="schedule-table"),
            Static(id="export-status"),
            id="schedule-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Called when screen is mounted."""
        self.load_schedules()
        table = self.query_one("#schedule-table", DataTable)
        # Row cursor: pin toggle [t] and the Enter explain act on the
        # highlighted section.
        table.cursor_type = "row"
        table.add_columns(
            "CRN", "Course", "Section", "Time", "Room", "Instructor",
            "Enrolled/Cap", "Pin"
        )
        self.show_current_schedule()

    def on_screen_resume(self) -> None:
        """Refresh when returning to this screen — MODES screens mount once,
        so data loaded in on_mount goes stale after a solve elsewhere."""
        self.load_schedules()
        self.show_current_schedule()

    def load_schedules(self) -> None:
        """Load all schedules."""
        engine = get_engine()
        with Session(engine) as session:
            self._schedules = list(
                session.exec(
                    select(ScheduleVersion).order_by(
                        ScheduleVersion.created_at.desc()  # type: ignore
                    )
                ).all()
            )
        # The list may have shrunk while this screen was suspended
        # (e.g. a schedule deleted from the web UI) — keep the index valid.
        self._current_index = min(self._current_index, max(0, len(self._schedules) - 1))

    def show_current_schedule(self) -> None:
        """Display the current schedule."""
        header = self.query_one("#schedule-header", Static)
        table = self.query_one("#schedule-table", DataTable)
        table.clear()
        # Reset alongside the table — a stale map after the schedule list
        # empties would let the pin toggle hit a row that no longer exists.
        self._row_section_ids = []

        if not self._schedules:
            header.update("[bold]No schedules found[/bold]")
            return

        schedule = self._schedules[self._current_index]
        header.update(
            f"[bold]{schedule.name}[/bold] ({schedule.semester}) - "
            f"{schedule.status.upper()} - "
            f"[{self._current_index + 1}/{len(self._schedules)}]"
        )

        engine = get_engine()
        with Session(engine) as session:
            sections = list(
                session.exec(
                    select(Section).where(Section.schedule_id == schedule.id)
                ).all()
            )

            time_slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
            rooms = {r.id: r for r in session.exec(select(Room)).all()}
            faculty = {f.id: f for f in session.exec(select(Faculty)).all()}

            # Bulk-fetch meeting 0 and enrollment counts — this renders on the
            # UI thread on every next/prev keypress; two queries per section
            # made it visibly laggy on real-size schedules.
            section_ids = [s.id for s in sections if s.id is not None]
            m0_by_section: dict[int, SectionMeeting] = {}
            enrolled_by_section: dict[int, int] = {}
            if section_ids:
                for sm in session.exec(
                    select(SectionMeeting).where(
                        SectionMeeting.section_id.in_(section_ids),
                        SectionMeeting.meeting_sequence == 0,
                    )
                ).all():
                    m0_by_section[sm.section_id] = sm
                enrolled_by_section = dict(
                    session.exec(
                        select(SectionEnrollment.section_id, func.count())
                        .where(SectionEnrollment.section_id.in_(section_ids))
                        .group_by(SectionEnrollment.section_id)
                    ).all()
                )

            self._row_section_ids = []
            for s in sections:
                m0 = m0_by_section.get(s.id)
                slot = time_slots.get(m0.time_slot_id) if m0 else None
                room = rooms.get(m0.room_id) if m0 else None
                fac = faculty.get(s.faculty_id)
                enrolled = enrolled_by_section.get(s.id, 0)

                table.add_row(
                    s.crn,
                    s.course_code,
                    s.section_number,
                    slot.name if slot else "TBA",
                    f"{room.building} {room.number}" if room else "TBA",
                    fac.name if fac else "TBA",
                    f"{enrolled}/{s.capacity}",
                    "📌" if s.is_pinned else "",
                )
                self._row_section_ids.append(s.id)

    def action_next_schedule(self) -> None:
        """Show next schedule."""
        if self._schedules and self._current_index < len(self._schedules) - 1:
            self._current_index += 1
            self.show_current_schedule()

    def action_prev_schedule(self) -> None:
        """Show previous schedule."""
        if self._schedules and self._current_index > 0:
            self._current_index -= 1
            self.show_current_schedule()

    def _get_current_schedule_id(self) -> int | None:
        """Return the ID of the currently displayed schedule, or None."""
        if not self._schedules:
            return None
        return self._schedules[self._current_index].id

    def _set_export_status(self, message: str) -> None:
        """Update the export status bar."""
        status = self.query_one("#export-status", Static)
        status.update(message)

    def action_viability(self) -> None:
        """Below-minimum sections and who gets stranded if each is cancelled."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule selected[/red]")
            return
        try:
            from src.core.viability import analyze_schedule_viability

            engine = get_engine()
            with Session(engine) as session:
                report = analyze_schedule_viability(session, schedule_id)
        except Exception as e:
            logger.exception("Viability check failed")
            self._set_export_status(f"[red]Viability check failed: {e}[/red]")
            return

        if not report.below_min:
            self._set_export_status(
                f"[green]All {report.total_sections} sections meet their "
                f"minimum enrollment.[/green]"
            )
            return
        lines = [
            f"[yellow bold]{len(report.below_min)} of {report.total_sections} "
            f"sections below minimum enrollment:[/yellow bold]"
        ]
        for sv in report.below_min:
            color = (
                "red" if sv.stranded_grad_critical
                else "yellow" if sv.stranded
                else "green"
            )
            lines.append(
                f"[{color}]{sv.course_code} {sv.section_number} "
                f"(CRN {sv.crn}): {sv.enrolled} enrolled, min "
                f"{sv.min_enrollment} — {sv.recommendation}[/{color}]"
            )
        self._set_export_status("\n".join(lines))

    def action_quality(self) -> None:
        """Student schedule quality: daily gaps, marathons, day-spread."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule selected[/red]")
            return
        try:
            from src.core.schedule_quality import analyze_student_quality

            engine = get_engine()
            with Session(engine) as session:
                report = analyze_student_quality(session, schedule_id)
        except Exception as e:
            logger.exception("Quality check failed")
            self._set_export_status(f"[red]Quality check failed: {e}[/red]")
            return

        if report.total_students == 0:
            self._set_export_status("[green]No enrolled students in this schedule.[/green]")
            return

        lines = [
            f"[bold]{report.total_students} students[/bold] — "
            f"mean {report.mean_days_on_campus:.1f} days on campus, "
            f"mean gap {report.mean_gap_minutes / 60:.1f}h — "
            f"{report.students_5_day} with 5-day spreads, "
            f"{report.students_big_gap} with 3h+ single gaps, "
            f"{report.students_heavy_marathon} with 3+ marathons over 3h, "
            f"{report.students_no_lunch} with no lunch break, "
            f"{report.students_long_workday} with 6h+ workdays",
            "[yellow bold]Worst 10 by badness score:[/yellow bold]",
        ]
        for sq in report.worst_students[:10]:
            color = (
                "red" if sq.badness >= 100
                else "yellow" if sq.badness >= 20
                else "green"
            )
            lines.append(
                f"[{color}]{sq.student_id}: {sq.days_on_campus} days, "
                f"{sq.total_gap_minutes / 60:.1f}h gap, "
                f"{sq.max_marathon_minutes / 60:.1f}h marathon, "
                f"{sq.earliest_start or '—'}–{sq.latest_end or '—'}[/{color}]"
            )
        self._set_export_status("\n".join(lines))

    def action_rebalance(self) -> None:
        """Pacing-aware seat rebalance: same timetable, better student weeks."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule selected[/red]")
            return
        from src.core import job_lock

        if not job_lock.try_acquire("rebalance", f"Rebalance {schedule_id}"):
            self._set_export_status(
                "[yellow]An optimization is running — try again after it "
                "finishes.[/yellow]"
            )
            return
        try:
            from src.core.seatswap import rebalance_seats

            engine = get_engine()
            with Session(engine) as session:
                r = rebalance_seats(session, schedule_id)
        except Exception as e:
            logger.exception("Rebalance failed")
            self._set_export_status(f"[red]Rebalance failed: {e}[/red]")
            return
        finally:
            job_lock.release()
        self._set_export_status(
            f"[green]Rebalanced: {r.moves} move(s), {r.students_improved} "
            f"student(s) improved, mean badness "
            f"{r.mean_badness_before} → {r.mean_badness_after}. "
            f"Press [bold]u[/bold] to see the new quality report.[/green]"
        )
        self.show_current_schedule()

    def action_export_csv(self) -> None:
        """Export current schedule as Banner CSV."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule to export[/red]")
            return
        try:
            from src.core.export import BannerExporter

            engine = get_engine()
            with Session(engine) as session:
                exporter = BannerExporter(session)
                out = EXPORT_DIR / f"schedule_{schedule_id}.csv"
                exporter.export_schedule(schedule_id, out)
            self._set_export_status(f"[green]Exported schedule CSV to {out}[/green]")
        except Exception as e:
            logger.exception("Export failed")
            self._set_export_status(f"[red]Export failed: {e}[/red]")

    def action_export_excel(self) -> None:
        """Export current schedule as Excel workbook (all sheets)."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule to export[/red]")
            return
        try:
            from src.core.export import BannerExporter

            engine = get_engine()
            with Session(engine) as session:
                exporter = BannerExporter(session)
                out = EXPORT_DIR / f"schedule_{schedule_id}.xlsx"
                exporter.export_excel(schedule_id, out)
            self._set_export_status(f"[green]Exported Excel workbook to {out}[/green]")
        except Exception as e:
            logger.exception("Export failed")
            self._set_export_status(f"[red]Export failed: {e}[/red]")

    def action_export_ical(self) -> None:
        """Write one .ics calendar per instructor and per room to exports/ical/."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule to export[/red]")
            return
        try:
            import re

            from sqlmodel import select

            from src.core.ical import ics_for_instructor, ics_for_room
            from src.database.models import Section, SectionMeeting

            out_dir = EXPORT_DIR / "ical" / f"schedule_{schedule_id}"
            out_dir.mkdir(parents=True, exist_ok=True)

            def safe(name: str) -> str:
                return re.sub(r"[^A-Za-z0-9._-]", "_", name)

            engine = get_engine()
            written = 0
            with Session(engine) as session:
                sections = list(session.exec(
                    select(Section).where(Section.schedule_id == schedule_id)
                ).all())
                sec_ids = [s.id for s in sections if s.id is not None]
                faculty_ids = sorted({s.faculty_id for s in sections if s.faculty_id})
                room_ids = sorted({
                    m.room_id
                    for m in session.exec(
                        select(SectionMeeting).where(
                            SectionMeeting.section_id.in_(sec_ids)  # type: ignore[union-attr]
                        )
                    ).all()
                }) if sec_ids else []
                for fid in faculty_ids:
                    (out_dir / f"instructor_{safe(fid)}.ics").write_text(
                        ics_for_instructor(session, schedule_id, fid)
                    )
                    written += 1
                for rid in room_ids:
                    (out_dir / f"room_{safe(rid)}.ics").write_text(
                        ics_for_room(session, schedule_id, rid)
                    )
                    written += 1
            self._set_export_status(
                f"[green]Wrote {written} .ics calendars "
                f"({len(faculty_ids)} instructors, {len(room_ids)} rooms) "
                f"to {out_dir}/[/green]"
            )
        except Exception as e:
            logger.exception("iCal export failed")
            self._set_export_status(f"[red]iCal export failed: {e}[/red]")

    def action_export_banner(self) -> None:
        """Export all Banner-format CSVs (schedule, enrollments, rooms, faculty)."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule to export[/red]")
            return
        try:
            from src.core.export import BannerExporter

            engine = get_engine()
            with Session(engine) as session:
                exporter = BannerExporter(session)
                files = exporter.export_all_csv(schedule_id, EXPORT_DIR)
            names = ", ".join(p.name for p in files.values())
            self._set_export_status(f"[green]Banner export to {EXPORT_DIR}/: {names}[/green]")
        except Exception as e:
            logger.exception("Export failed")
            self._set_export_status(f"[red]Export failed: {e}[/red]")

    def _cursor_section_id(self) -> int | None:
        """Section id of the highlighted table row, or None."""
        table = self.query_one("#schedule-table", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._row_section_ids)):
            return None
        return self._row_section_ids[row]

    def action_toggle_pin(self) -> None:
        """Pin/unpin the highlighted section (kept fixed on re-solves)."""
        section_id = self._cursor_section_id()
        if section_id is None:
            self._set_export_status("[red]No section highlighted[/red]")
            return
        table = self.query_one("#schedule-table", DataTable)
        cursor_row = table.cursor_row
        engine = get_engine()
        with Session(engine) as session:
            section = session.get(Section, section_id)
            if section is None:
                self._set_export_status("[red]Section no longer exists[/red]")
                return
            if section.is_pinned:
                section.is_pinned = False
                section.pin_reason = None
                msg = (f"[yellow]Unpinned {section.course_code} "
                       f"{section.section_number} — it may move on the next "
                       f"re-solve.[/yellow]")
            else:
                section.is_pinned = True
                section.pin_reason = "Pinned in TUI"
                msg = (f"[green]Pinned {section.course_code} "
                       f"{section.section_number} — re-solves keep its "
                       f"time/room/instructor.[/green]")
            session.add(section)
            session.commit()
        self.show_current_schedule()
        if cursor_row is not None:
            table.move_cursor(row=cursor_row)
        self._set_export_status(msg)

    def _hand_off_solve(self, kind: str, verb: str) -> None:
        """Acquire the job lock and hand a re-solve/repair to the dashboard,
        which owns the solver log + live viz (see its on_screen_resume)."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule selected[/red]")
            return
        from src.core import job_lock

        if not job_lock.try_acquire(kind, f"TUI {verb} {schedule_id}"):
            self._set_export_status(
                "[yellow]An optimization is already running — try again "
                "after it finishes.[/yellow]"
            )
            return
        self.app.pending_job = (kind, schedule_id)
        self.app.switch_mode("dashboard")

    def action_resolve(self) -> None:
        """Re-solve this schedule: pinned sections stay, the rest re-optimize."""
        self._hand_off_solve("resolve", "re-solve")

    def action_repair(self) -> None:
        """Minimal-change repair against current data (resigned instructor,
        closed room…): everything is anchored, only forced changes move."""
        self._hand_off_solve("repair", "repair")

    def action_diff_prev(self) -> None:
        """Diff this schedule against the next-older one in the list."""
        if len(self._schedules) < 2 or self._current_index >= len(self._schedules) - 1:
            self._set_export_status(
                "[yellow]No older schedule to compare against.[/yellow]")
            return
        current = self._schedules[self._current_index]
        older = self._schedules[self._current_index + 1]
        try:
            from src.core.schedule_diff import diff_schedules

            engine = get_engine()
            with Session(engine) as session:
                diff = diff_schedules(session, current.id, base_id=older.id)
        except Exception as e:
            logger.exception("Diff failed")
            self._set_export_status(f"[red]Diff failed: {e}[/red]")
            return

        lines = [
            f"[bold]{current.name} vs {older.name}:[/bold] "
            f"{diff['unchanged_count']} unchanged, "
            f"{len(diff['changed'])} changed, {len(diff['added'])} added, "
            f"{len(diff['removed'])} removed"
        ]
        for c in diff["changed"][:15]:
            moves = "; ".join(
                f"{fieldname}: {d['from'] or '—'} → {d['to'] or '—'}"
                for fieldname, d in c["changes"].items()
            )
            lines.append(f"[yellow]~ {c['course_code']}-{c['section_number']}: "
                         f"{moves}[/yellow]")
        for a in diff["added"][:5]:
            lines.append(f"[green]+ {a['course_code']}-{a['section_number']}[/green]")
        for r in diff["removed"][:5]:
            lines.append(f"[red]- {r['course_code']}-{r['section_number']}[/red]")
        shown = min(15, len(diff["changed"])) + min(5, len(diff["added"])) \
            + min(5, len(diff["removed"]))
        total = len(diff["changed"]) + len(diff["added"]) + len(diff["removed"])
        if total > shown:
            lines.append(f"[dim]… and {total - shown} more[/dim]")
        self._set_export_status("\n".join(lines))

    def action_check_conflicts(self) -> None:
        """Hard-constraint check: room/faculty double-books, student clashes."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule selected[/red]")
            return
        try:
            from src.core.validate import check_schedule_conflicts

            engine = get_engine()
            with Session(engine) as session:
                report = check_schedule_conflicts(session, schedule_id)
        except Exception as e:
            logger.exception("Conflict check failed")
            self._set_export_status(f"[red]Conflict check failed: {e}[/red]")
            return

        if report.total == 0:
            self._set_export_status(
                "[green]No hard conflicts: rooms, instructors, and student "
                "schedules are clean.[/green]")
            return
        lines = [
            f"[red bold]{report.total} conflict(s)[/red bold] — "
            f"{report.room} room, {report.faculty} faculty, "
            f"{report.student} student"
        ]
        for c in report.conflicts[:12]:
            secs = ", ".join(f"{s.course_code}-{s.section_number}"
                             for s in c.sections)
            students = (f" ({c.affected_students} students)"
                        if c.affected_students else "")
            lines.append(f"[red]{c.kind}: {c.detail} — {secs}{students}[/red]")
        if report.total > 12:
            lines.append(f"[dim]… and {report.total - 12} more[/dim]")
        self._set_export_status("\n".join(lines))

    def action_recommendations(self) -> None:
        """Improvement suggestions: overloads, imbalances, utilization."""
        schedule_id = self._get_current_schedule_id()
        if schedule_id is None:
            self._set_export_status("[red]No schedule selected[/red]")
            return
        try:
            from src.core.recommend import analyze_schedule

            threshold = (self.app.conflict_threshold
                         if self.app.conflict_threshold >= 0 else 50.0)
            engine = get_engine()
            with Session(engine) as session:
                recs = analyze_schedule(session, schedule_id,
                                        conflict_threshold=threshold)
        except Exception as e:
            logger.exception("Recommendations failed")
            self._set_export_status(f"[red]Recommendations failed: {e}[/red]")
            return

        if not recs:
            self._set_export_status(
                "[green]No recommendations — nothing obvious to improve.[/green]")
            return
        lines = [f"[bold]{len(recs)} recommendation(s), most important "
                 f"first:[/bold]"]
        for r in recs[:10]:
            kind = getattr(r.type, "value", str(r.type))
            lines.append(
                f"[yellow]• {kind} (priority {r.priority:.0f}): "
                f"{r.description} → {r.suggested_action}[/yellow]"
            )
        if len(recs) > 10:
            lines.append(f"[dim]… and {len(recs) - 10} more[/dim]")
        self._set_export_status("\n".join(lines))

    def on_data_table_row_selected(self, event) -> None:
        """Enter on a row: explain why the section is placed where it is."""
        section_id = self._cursor_section_id()
        schedule_id = self._get_current_schedule_id()
        if section_id is None or schedule_id is None:
            return
        try:
            from src.core.advisor import explain_section

            engine = get_engine()
            with Session(engine) as session:
                result = explain_section(session, schedule_id, section_id)
        except Exception as e:
            logger.exception("Explain failed")
            self._set_export_status(f"[red]Explain failed: {e}[/red]")
            return
        reasons = result.get("reasons") or []
        header = f"[bold]Why is CRN {result.get('crn', '?')} placed here?[/bold]"
        lines = [header] + [f"  • {r}" for r in reasons]
        self._set_export_status("\n".join(lines))

    def action_switch_mode(self, mode: str) -> None:
        """Switch to a different screen."""
        self.app.switch_mode(mode)

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()
