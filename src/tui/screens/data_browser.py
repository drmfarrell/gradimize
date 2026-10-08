"""Data browser screen for the TUI."""

import logging

from sqlmodel import Session, select
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from src.core.offerings import infer_course_offerings
from src.database.connection import get_engine
from src.database.models import Course, Faculty, FreshmenProjection, Room, Student

logger = logging.getLogger(__name__)


class DataBrowserScreen(Screen):
    """Data browsing screen with switchable views."""

    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("s", "switch_mode('schedule')", "Schedule"),
        ("c", "switch_mode('conflicts')", "Conflicts"),
        ("f", "switch_mode('finals')", "Finals"),
        ("w", "switch_mode('settings')", "Settings"),
        ("m", "switch_mode('import')", "Import"),
        ("1", "show_students", "Students"),
        ("2", "show_courses", "Courses"),
        ("3", "show_faculty", "Faculty"),
        ("4", "show_rooms", "Rooms"),
        ("5", "show_projections", "Projections"),
        ("i", "infer_offerings", "Infer Offerings"),
        ("x", "delete_projection", "Delete Projection"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    DataBrowserScreen {
        layout: vertical;
    }

    #browser-container {
        height: 100%;
        width: 100%;
        padding: 1;
    }

    #browser-header {
        height: 3;
        width: 100%;
        padding: 0 1;
        background: $primary;
    }

    #data-table {
        height: 1fr;
        width: 100%;
        border: solid blue;
    }

    #browser-status {
        height: 1;
        width: 100%;
        padding: 0 1;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_view = "students"

    def compose(self) -> ComposeResult:
        """Compose the screen."""
        yield Header()
        yield Container(
            Static(
                "\\[1] Students  \\[2] Courses  \\[3] Faculty  \\[4] Rooms  "
                "\\[5] Projections  \\[I] Infer Offerings",
                id="browser-header",
            ),
            DataTable(id="data-table"),
            Static(id="browser-status"),
            id="browser-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Called when screen is mounted."""
        self.show_students()

    def on_screen_resume(self) -> None:
        """Re-show the active view when returning — MODES screens mount once,
        so data loaded in on_mount goes stale after imports/solves elsewhere."""
        views = {
            "students": self.show_students,
            "courses": self.show_courses,
            "faculty": self.show_faculty,
            "rooms": self.show_rooms,
            "projections": self.show_projections,
        }
        views.get(getattr(self, "_current_view", "students"), self.show_students)()

    def _clear_table(self) -> DataTable:
        """Clear and return the data table."""
        table = self.query_one("#data-table", DataTable)
        table.clear(columns=True)
        return table

    def _update_header(self, view_name: str) -> None:
        """Update the header to show current view."""
        header = self.query_one("#browser-header", Static)
        suffix = "  \\[I] Infer Offerings"
        names = [("students", "\\[1] Students"), ("courses", "\\[2] Courses"),
                 ("faculty", "\\[3] Faculty"), ("rooms", "\\[4] Rooms"),
                 ("projections", "\\[5] Projections")]
        parts = [
            f"[reverse] {label} [/reverse]" if key == view_name else label
            for key, label in names
        ]
        header.update("  ".join(parts) + suffix)

    def _set_status(self, message: str) -> None:
        try:
            self.query_one("#browser-status", Static).update(message)
        except Exception:
            pass

    def action_show_students(self) -> None:
        """Show students view."""
        self.show_students()

    def show_students(self) -> None:
        """Display students data."""
        self._current_view = "students"
        self._update_header("students")
        table = self._clear_table()
        table.add_columns("ID", "Name", "Standing", "Major", "Credits")

        engine = get_engine()
        with Session(engine) as session:
            students = list(session.exec(select(Student).limit(100)).all())
            for s in students:
                table.add_row(
                    s.id, s.name, s.class_standing.value, s.major, str(s.credits_earned)
                )

    def action_show_courses(self) -> None:
        """Show courses view."""
        self.show_courses()

    def show_courses(self) -> None:
        """Display courses data."""
        self._current_view = "courses"
        self._update_header("courses")
        table = self._clear_table()
        table.add_columns(
            "Code", "Title", "Credits", "Type", "Room Types",
            "Offer Terms", "Cadence",
        )

        engine = get_engine()
        with Session(engine) as session:
            courses = list(session.exec(select(Course)).all())
            for c in courses:
                table.add_row(
                    c.code,
                    c.title[:40],
                    str(c.credits),
                    c.structure_type.value,
                    ", ".join(c.required_room_types[:2]),
                    ",".join(c.offer_terms) if c.offer_terms else "any",
                    c.cadence or "ANNUAL",
                )

    def action_show_faculty(self) -> None:
        """Show faculty view."""
        self.show_faculty()

    def show_faculty(self) -> None:
        """Display faculty data."""
        self._current_view = "faculty"
        self._update_header("faculty")
        table = self._clear_table()
        table.add_columns("ID", "Name", "Email", "Max Credits", "Approved Courses")

        engine = get_engine()
        with Session(engine) as session:
            faculty = list(session.exec(select(Faculty)).all())
            for f in faculty:
                courses_str = ", ".join(f.approved_courses[:3])
                if len(f.approved_courses) > 3:
                    courses_str += "..."
                table.add_row(
                    f.id, f.name, f.email, str(f.max_credits), courses_str
                )

    def action_show_rooms(self) -> None:
        """Show rooms view."""
        self.show_rooms()

    def show_rooms(self) -> None:
        """Display rooms data."""
        self._current_view = "rooms"
        self._update_header("rooms")
        table = self._clear_table()
        table.add_columns("ID", "Building", "Room", "Capacity", "Type")

        engine = get_engine()
        with Session(engine) as session:
            rooms = list(session.exec(select(Room)).all())
            for r in rooms:
                table.add_row(
                    r.id, r.building, r.number, str(r.capacity), r.room_type.value
                )

    def action_show_projections(self) -> None:
        """Show freshmen projections view."""
        self.show_projections()

    def show_projections(self) -> None:
        """Display freshmen projections (projected incoming-class demand)."""
        self._current_view = "projections"
        self._update_header("projections")
        table = self._clear_table()
        # Row cursor so [X] can delete the highlighted projection.
        table.cursor_type = "row"
        table.add_columns("ID", "Major", "Est. Count", "Catalog Year", "Active")
        self._projection_row_ids: list[int] = []

        engine = get_engine()
        with Session(engine) as session:
            projections = list(session.exec(select(FreshmenProjection)).all())
            for p in projections:
                table.add_row(
                    str(p.id), p.major, str(p.estimated_count),
                    str(p.catalog_year), "yes" if p.is_active else "no",
                )
                self._projection_row_ids.append(p.id)
        self._set_status(
            f"{len(self._projection_row_ids)} projection(s). "
            "\\[X] deletes the highlighted one; create via Import (\\[M], "
            "freshmen_projections.csv). Generate uses them when "
            "'Include projections' is on in Settings."
        )

    def action_delete_projection(self) -> None:
        """Delete the highlighted projection (projections view only)."""
        if self._current_view != "projections":
            self._set_status(
                "[yellow]\\[X] deletes projections — switch to view \\[5] "
                "first.[/yellow]")
            return
        table = self.query_one("#data-table", DataTable)
        row = table.cursor_row
        ids = getattr(self, "_projection_row_ids", [])
        if row is None or not (0 <= row < len(ids)):
            self._set_status("[red]No projection highlighted[/red]")
            return
        engine = get_engine()
        with Session(engine) as session:
            p = session.get(FreshmenProjection, ids[row])
            if p is None:
                self._set_status("[red]Projection no longer exists[/red]")
                return
            label = f"{p.major}/{p.catalog_year} ({p.estimated_count})"
            session.delete(p)
            session.commit()
        self.show_projections()
        self._set_status(f"[green]Deleted projection {label}[/green]")

    def action_infer_offerings(self) -> None:
        """Infer offer_terms + cadence for every course from historical schedules."""
        self._set_status(
            "[yellow]Inferring offering patterns from historical schedules…[/yellow]"
        )
        self.run_worker(self._do_infer_offerings, thread=True)

    def _do_infer_offerings(self) -> None:
        try:
            engine = get_engine()
            with Session(engine) as session:
                report = infer_course_offerings(session, apply=True)
            msg = (
                f"[green]Inferred from {report.schedules_examined} schedules: "
                f"{report.courses_changed} of {report.courses_with_history} "
                f"courses with history updated "
                f"({report.courses_examined} total examined).[/green]"
            )
            if report.skipped_unparseable_semesters:
                msg += (
                    f" [yellow]Skipped {len(report.skipped_unparseable_semesters)} "
                    f"unparseable semester codes.[/yellow]"
                )
            self.app.call_from_thread(self._set_status, msg)
            if self._current_view == "courses":
                self.app.call_from_thread(self.show_courses)
        except Exception as e:
            logger.exception("Inference failed")
            self.app.call_from_thread(
                self._set_status, f"[red]Inference failed: {e}[/red]"
            )

    def action_switch_mode(self, mode: str) -> None:
        """Switch to a different screen."""
        self.app.switch_mode(mode)

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()
