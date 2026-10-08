"""Folder-based data import screen for the TUI.

Point this screen at a folder ("plop files in a folder"), it shows which
canonical import files are present, and \\[I] imports every present kind in
IMPORT_KINDS order.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from src.core.uploads import IMPORT_KINDS, import_data_file
from src.database.connection import get_engine

REPO_ROOT = Path(__file__).resolve().parents[3]

logger = logging.getLogger(__name__)


def _default_import_dir() -> Path:
    """Default folder to watch for import files."""
    data_dir = os.environ.get("GRADIMIZE_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "incoming"
    return REPO_ROOT / "data" / "incoming"


def scan_import_folder(folder: Path) -> tuple[list[dict], list[str]]:
    """Scan `folder` for the canonical import files.

    Returns:
        (rows, unrecognized) where `rows` has one dict per ImportKind (in
        IMPORT_KINDS order) with keys key/label/filename/exists/size/mtime,
        and `unrecognized` is a sorted list of filenames in the folder that
        match no canonical filename (subdirectories and dotfiles ignored).
    """
    canonical = {kind.filename for kind in IMPORT_KINDS}
    present: dict[str, Path] = {}
    if folder.is_dir():
        for entry in folder.iterdir():
            if entry.is_file() and not entry.name.startswith("."):
                present[entry.name] = entry

    rows: list[dict] = []
    for kind in IMPORT_KINDS:
        path = present.get(kind.filename)
        if path is not None:
            stat = path.stat()
            rows.append({
                "key": kind.key,
                "label": kind.label,
                "filename": kind.filename,
                "exists": True,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
        else:
            rows.append({
                "key": kind.key,
                "label": kind.label,
                "filename": kind.filename,
                "exists": False,
                "size": None,
                "mtime": None,
            })

    unrecognized = sorted(name for name in present if name not in canonical)
    return rows, unrecognized


class ImportScreen(Screen):
    """Folder-watch data import screen."""

    # Without this, the folder Input is the first focusable widget and grabs
    # focus on mount — every letter key (including I/R and the nav keys)
    # would be typed into the path instead of triggering bindings.
    AUTO_FOCUS = "#import-table"

    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("s", "switch_mode('schedule')", "Schedule"),
        ("c", "switch_mode('conflicts')", "Conflicts"),
        ("b", "switch_mode('browser')", "Data Browser"),
        ("f", "switch_mode('finals')", "Finals"),
        ("w", "switch_mode('settings')", "Settings"),
        ("r", "rescan", "Rescan"),
        ("i", "import_all", "Import All"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    ImportScreen {
        layout: vertical;
    }

    #import-container {
        height: 100%;
        width: 100%;
        padding: 1;
    }

    #import-title {
        height: 1;
        width: 100%;
        padding: 0 1;
    }

    #import-folder {
        height: 3;
        width: 100%;
    }

    #import-table {
        height: 1fr;
        width: 100%;
        border: solid blue;
    }

    #import-log {
        height: 1fr;
        width: 100%;
        border: solid green;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the screen."""
        yield Header()
        yield Container(
            Static("[bold]Data Import[/bold] — folder-watch import", id="import-title"),
            Input(value=str(_default_import_dir()), id="import-folder"),
            DataTable(id="import-table"),
            RichLog(id="import-log", markup=True),
            id="import-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Called when screen is mounted."""
        table = self.query_one("#import-table", DataTable)
        table.add_columns("File", "Kind", "Status")
        self.rescan()

    def on_screen_resume(self) -> None:
        """Refresh when returning to this screen."""
        self.rescan()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Rescan when the folder path is edited and submitted."""
        if event.input.id == "import-folder":
            self.rescan()
            # Hand focus back to the table so the letter bindings work
            # again without a manual Tab.
            self.query_one("#import-table", DataTable).focus()

    def _current_folder(self) -> Path:
        return Path(self.query_one("#import-folder", Input).value.strip())

    def action_rescan(self) -> None:
        """Rescan binding (\\[R])."""
        self.rescan()

    def rescan(self, preserve_log: bool = False) -> None:
        """Rebuild the import table from the current folder.

        preserve_log=True refreshes only the table — used right after an
        import finishes, when clearing the log would wipe the per-file
        results the user is reading.
        """
        folder = self._current_folder()
        rows, unrecognized = scan_import_folder(folder)

        table = self.query_one("#import-table", DataTable)
        table.clear()
        for row in rows:
            if row["exists"]:
                mtime = datetime.fromtimestamp(row["mtime"]).strftime("%Y-%m-%d %H:%M")
                status = f"{row['size']:,} bytes · {mtime}"
            else:
                status = "[dim]not found[/dim]"
            table.add_row(row["filename"], row["label"], status)

        if preserve_log:
            return
        log = self.query_one("#import-log", RichLog)
        log.clear()
        if not folder.is_dir():
            log.write(
                f"[yellow]Folder {folder} does not exist yet — it will be "
                "read once created.[/yellow]"
            )
        if unrecognized:
            shown = unrecognized[:10]
            extra = len(unrecognized) - len(shown)
            msg = "Unrecognized files: " + ", ".join(shown)
            if extra > 0:
                msg += f" … and {extra} more"
            log.write(f"[dim]{msg}[/dim]")

    def _write_log(self, message: str) -> None:
        """Write a message to the import log (thread-safe)."""
        self.app.call_from_thread(self._append_log, message)

    def _append_log(self, message: str) -> None:
        """Append a message to the import log (must be called from main thread)."""
        try:
            log = self.query_one("#import-log", RichLog)
            log.write(message)
        except Exception:
            pass

    def action_import_all(self) -> None:
        """Import every canonical file present in the folder, in order."""
        from src.core import job_lock

        log = self.query_one("#import-log", RichLog)
        if not job_lock.try_acquire("import", "TUI Import"):
            log.write(
                "[yellow]An optimization or import is already running — "
                "wait for it to finish before starting another.[/yellow]"
            )
            return
        log.write("[yellow bold]Starting import...[/yellow bold]")
        # Read the folder path on the main thread — the worker must not
        # touch widgets (Textual threading contract).
        self._import_folder = self._current_folder()
        self.run_worker(self._do_import_all, thread=True)

    def _do_import_all(self) -> None:
        """Import every present kind (worker thread). Releases job_lock when done."""
        from sqlmodel import Session

        from src.core import job_lock

        folder = self._import_folder
        ok = failed = 0
        try:
            for kind in IMPORT_KINDS:
                path = folder / kind.filename
                if not path.is_file():
                    continue
                try:
                    content = path.read_bytes()
                    with Session(get_engine()) as session:
                        result = import_data_file(
                            session, kind.key, content, replace=True)
                except Exception as e:
                    # A vanished/unreadable file must not abort the rest of
                    # the delivery.
                    logger.exception("Import failed")
                    failed += 1
                    self._write_log(
                        f"[red bold]{kind.filename}: import failed "
                        f"({type(e).__name__}: {e})[/red bold]")
                    continue

                if result.errors:
                    failed += 1
                    self._write_log(
                        f"[red bold]{kind.filename}: {result.rows_loaded} rows "
                        f"loaded, {len(result.errors)} error(s)[/red bold]"
                    )
                    for err in result.errors[:5]:
                        self._write_log(f"[red]  - {err}[/red]")
                    extra = len(result.errors) - 5
                    if extra > 0:
                        self._write_log(f"[red]  … and {extra} more[/red]")
                else:
                    ok += 1
                    self._write_log(
                        f"[green]{kind.filename}: {result.rows_loaded} rows loaded"
                        f"{' (replaced)' if result.cleared else ''}[/green]"
                    )
        finally:
            job_lock.release()

        summary = f"[bold]{ok} kind(s) imported cleanly"
        if failed:
            summary += f", [red]{failed} with errors[/red]"
        self._write_log(summary + ".[/bold]")
        self._write_log(
            "[dim]Press \\[D] then \\[H] to run the data health check.[/dim]"
        )
        self.app.call_from_thread(lambda: self.rescan(preserve_log=True))

    def action_switch_mode(self, mode: str) -> None:
        """Switch to a different screen."""
        self.app.switch_mode(mode)

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()
