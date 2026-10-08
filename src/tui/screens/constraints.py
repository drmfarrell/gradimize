"""Conflicts/constraints viewer screen for the TUI."""

from sqlmodel import Session
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from src.core.conflicts import ConflictMatrix
from src.database.connection import get_engine


class ConflictsScreen(Screen):
    """Conflicts viewing screen."""

    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("s", "switch_mode('schedule')", "Schedule"),
        ("b", "switch_mode('browser')", "Data Browser"),
        ("f", "switch_mode('finals')", "Finals"),
        ("w", "switch_mode('settings')", "Settings"),
        ("m", "switch_mode('import')", "Import"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    ConflictsScreen {
        layout: vertical;
    }

    #conflicts-container {
        height: 100%;
        width: 100%;
        padding: 1;
    }

    #conflicts-header {
        height: 3;
        width: 100%;
        padding: 0 1;
    }

    #conflicts-table {
        height: 1fr;
        width: 100%;
        border: solid yellow;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the screen."""
        yield Header()
        yield Container(
            Static(id="conflicts-header"),
            DataTable(id="conflicts-table"),
            id="conflicts-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Called when screen is mounted."""
        table = self.query_one("#conflicts-table", DataTable)
        table.add_columns("Course 1", "Course 2", "Conflict Score", "Severity")
        self.load_conflicts()

    def load_conflicts(self) -> None:
        """Load and display conflicts."""
        header = self.query_one("#conflicts-header", Static)
        table = self.query_one("#conflicts-table", DataTable)
        table.clear()

        engine = get_engine()
        with Session(engine) as session:
            matrix = ConflictMatrix(session)
            matrix.build_matrix()

            if matrix.matrix is None or matrix.matrix.empty:
                header.update(
                    "[bold]Course Conflicts[/bold] - No data (run demand calculation first)"
                )
                return

            pairs = matrix.get_high_conflict_pairs(threshold=25.0)[:50]
            header.update(
                f"[bold]Course Conflicts[/bold] - "
                f"{len(matrix.matrix.index)} courses, {len(pairs)} high-conflict pairs"
            )

            for c1, c2, score in pairs:
                if score >= 100:
                    severity = "[bold red]Critical[/bold red]"
                elif score >= 75:
                    severity = "[red]High[/red]"
                elif score >= 50:
                    severity = "[yellow]Medium[/yellow]"
                else:
                    severity = "[green]Low[/green]"

                table.add_row(c1, c2, f"{score:.1f}", severity)

    def action_refresh(self) -> None:
        """Refresh the conflicts display."""
        self.load_conflicts()

    def action_switch_mode(self, mode: str) -> None:
        """Switch to a different screen."""
        self.app.switch_mode(mode)

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()
