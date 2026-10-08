"""Settings screen for the TUI."""

from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Key
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class SettingsScreen(Screen):
    """Solver settings screen."""

    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("s", "switch_mode('schedule')", "Schedule"),
        ("c", "switch_mode('conflicts')", "Conflicts"),
        ("b", "switch_mode('browser')", "Data Browser"),
        ("f", "switch_mode('finals')", "Finals"),
        ("m", "switch_mode('import')", "Import"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        layout: vertical;
    }

    #settings-container {
        height: 100%;
        width: 100%;
        padding: 1;
    }

    #settings-panel {
        margin: 1;
        padding: 1;
        border: solid green;
        width: 60;
        height: auto;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._editing: str | None = None
        self._buffer: str = ""

    def compose(self) -> ComposeResult:
        """Compose the settings screen."""
        yield Header()
        yield Container(
            Container(
                Static("[bold]Solver Settings[/bold]\n", id="settings-header"),
                Static(id="settings-display"),
                Static(id="edit-prompt"),
                id="settings-panel",
            ),
            id="settings-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Show current values."""
        self._editing = None
        self._buffer = ""
        self.query_one("#edit-prompt", Static).update("")
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Update the display with current settings."""
        display = self.query_one("#settings-display", Static)
        cap = self.app.class_size_cap
        mode = self.app.capacity_mode
        conflict_threshold = (
            self.app.conflict_threshold if self.app.conflict_threshold >= 0 else "config default"
        )
        display.update(
            f"  [1] Solver workers:        {self.app.solver_workers}"
            f"{' (auto)' if self.app.solver_workers == 0 else ''}\n"
            f"  [2] Time limit (seconds):  {self.app.solver_time}\n"
            f"  [3] Semester:              {self.app.semester}\n"
            f"  [4] Class size cap:        {cap if cap else 'config default'}\n"
            f"  [5] Capacity mode:         {mode if mode else 'config default'}"
            f"{' — sizes float to the room' if mode == 'room' else ''}\n"
            f"  [6] Conflict threshold:    {conflict_threshold}"
            f" (lower = stricter)\n"
            f"  [7] Include projections:   "
            f"{'yes' if self.app.include_projections else 'no'}"
            f" — fold projected freshmen demand into Generate\n"
        )

    def _update_prompt(self) -> None:
        """Update the edit prompt with current buffer."""
        prompt = self.query_one("#edit-prompt", Static)
        if self._editing == "workers":
            prompt.update(f"  New worker count (0 = all cores): {self._buffer}_")
        elif self._editing == "time":
            prompt.update(f"  New time limit (seconds): {self._buffer}_")
        elif self._editing == "semester":
            prompt.update(f"  New semester (e.g. 2026FA): {self._buffer}_")
        elif self._editing == "cap":
            prompt.update(
                f"  Max seats per class (0 = config default): {self._buffer}_"
            )
        elif self._editing == "conflict":
            prompt.update(
                "  Conflict threshold — pairs above this urgency sum are "
                f"penalized; lower = stricter (blank = config default): {self._buffer}_"
            )

    def on_key(self, event: Key) -> None:
        """Handle key presses for editing mode."""
        if self._editing is None:
            if event.key == "1":
                self._editing = "workers"
                self._buffer = ""
                self._update_prompt()
                event.prevent_default()
            elif event.key == "2":
                self._editing = "time"
                self._buffer = ""
                self._update_prompt()
                event.prevent_default()
            elif event.key == "3":
                self._editing = "semester"
                self._buffer = ""
                self._update_prompt()
                event.prevent_default()
            elif event.key == "4":
                self._editing = "cap"
                self._buffer = ""
                self._update_prompt()
                event.prevent_default()
            elif event.key == "5":
                # Cycle capacity mode in place — no text entry needed.
                order = ["", "fixed", "room"]
                current = self.app.capacity_mode
                idx = order.index(current) if current in order else 0
                self.app.capacity_mode = order[(idx + 1) % len(order)]
                self._refresh_display()
                event.prevent_default()
            elif event.key == "6":
                self._editing = "conflict"
                self._buffer = ""
                self._update_prompt()
                event.prevent_default()
            elif event.key == "7":
                # Boolean toggle in place — no text entry needed.
                self.app.include_projections = not self.app.include_projections
                self._refresh_display()
                event.prevent_default()
            return

        # In editing mode
        event.prevent_default()

        if event.key == "enter":
            prompt = self.query_one("#edit-prompt", Static)
            if self._editing == "workers":
                # 0 is valid: "use all cores" (an earlier check required >= 1,
                # so auto mode was unreachable once changed).
                if self._buffer.isdigit():
                    self.app.solver_workers = int(self._buffer)
                elif self._buffer:
                    prompt.update("  [red]Workers must be a number >= 0[/red]")
            elif self._editing == "time":
                if self._buffer.isdigit() and int(self._buffer) >= 1:
                    self.app.solver_time = int(self._buffer)
                elif self._buffer:
                    prompt.update("  [red]Time limit must be a number >= 1[/red]")
            elif self._editing == "semester":
                from src.core.timeutil import parse_semester
                try:
                    parse_semester(self._buffer)
                    self.app.semester = self._buffer.strip().upper()
                except ValueError:
                    prompt.update("  [red]Invalid semester (use e.g. 2026FA)[/red]")
            elif self._editing == "cap":
                # 0 is valid: "use the config default".
                if self._buffer.isdigit():
                    self.app.class_size_cap = int(self._buffer)
                elif self._buffer:
                    prompt.update("  [red]Cap must be a number >= 0[/red]")
            elif self._editing == "conflict":
                # Blank = back to config default; 0 is valid (penalize every
                # pair with any shared urgency at all).
                if self._buffer.isdigit():
                    self.app.conflict_threshold = float(self._buffer)
                else:
                    self.app.conflict_threshold = -1.0
            self._editing = None
            self._buffer = ""
            self.query_one("#edit-prompt", Static).update("")
            self._refresh_display()
        elif event.key == "escape":
            self._editing = None
            self._buffer = ""
            self.query_one("#edit-prompt", Static).update("")
        elif event.key == "backspace" and self._buffer:
            self._buffer = self._buffer[:-1]
            self._update_prompt()
        elif len(event.key) == 1 and (event.key.isdigit() or
                                      (self._editing == "semester" and event.key.isalpha())):
            self._buffer += event.key.upper() if self._editing == "semester" else event.key
            self._update_prompt()

    def action_switch_mode(self, mode: str) -> None:
        """Switch to a different screen."""
        self.app.switch_mode(mode)

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()
