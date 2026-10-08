"""Headless TUI smoke test: every mode mounts without crashing.

The TUI screens had no widget-level tests at all — regressions surfaced
only when a human ran the app. This drives the real Textual app through
every registered mode with the pilot, which catches the whole class of
"screen references a widget/action that doesn't exist" errors, including
wiring mistakes in newly added screens (import) and bindings.

Uses a scratch database via a temp GRADIMIZE_DATA_DIR so the app's
create_tables touches nothing real.
"""

import pytest

pytest.importorskip("textual")


@pytest.fixture
def scratch_app(tmp_path, monkeypatch):
    """App against a scratch DB.

    Setting GRADIMIZE_DATA_DIR here would be a no-op: connection.py bakes
    it into DEFAULT_DB_PATH at import time (and conftest strips it before
    that import). Patch DEFAULT_DB_PATH itself instead — get_engine()
    resolves it at CALL time and caches engines per path, so a fresh tmp
    path always gets a fresh engine. Without this, the pilot's keypresses
    land on the real demo database (a 't' press once pinned a demo
    section; an 'o' press once started a real re-solve).
    """
    import src.database.connection as conn

    monkeypatch.setattr(conn, "DEFAULT_DB_PATH", tmp_path / "scratch.db")
    from src.tui.app import GradimizeApp

    return GradimizeApp()


@pytest.mark.asyncio
async def test_all_modes_mount(scratch_app):
    async with scratch_app.run_test() as pilot:
        for mode in scratch_app.MODES:
            scratch_app.switch_mode(mode)
            await pilot.pause()
        assert scratch_app.current_mode == list(scratch_app.MODES)[-1]


@pytest.mark.asyncio
async def test_schedule_screen_actions_on_empty_db(scratch_app):
    """Actions on an empty DB must degrade to a status message, not crash."""
    async with scratch_app.run_test() as pilot:
        scratch_app.switch_mode("schedule")
        await pilot.pause()
        for key in ("t", "o", "k", "y", "z", "j", "v", "u"):
            await pilot.press(key)
            await pilot.pause()


@pytest.mark.asyncio
async def test_browser_views_and_projection_delete(scratch_app):
    """All browser views mount; [x] outside/inside the empty projections
    view degrades to a status message."""
    async with scratch_app.run_test() as pilot:
        scratch_app.switch_mode("browser")
        await pilot.pause()
        for key in ("1", "2", "3", "4", "x", "5", "x"):
            await pilot.press(key)
            await pilot.pause()


@pytest.mark.asyncio
async def test_settings_projection_toggle(scratch_app):
    async with scratch_app.run_test() as pilot:
        scratch_app.switch_mode("settings")
        await pilot.pause()
        assert scratch_app.include_projections is False
        await pilot.press("7")
        await pilot.pause()
        assert scratch_app.include_projections is True
        await pilot.press("7")
        await pilot.pause()
        assert scratch_app.include_projections is False


@pytest.mark.asyncio
async def test_import_screen_scans_missing_folder(scratch_app):
    async with scratch_app.run_test() as pilot:
        scratch_app.switch_mode("import")
        await pilot.pause()
        # Focus must land on the TABLE, not the folder Input — otherwise
        # every letter binding (I/R/nav keys) types into the path instead.
        focused = scratch_app.focused
        assert focused is not None and focused.id == "import-table"
        await pilot.press("r")  # rescan of a nonexistent folder must not crash
        await pilot.pause()
        await pilot.press("i")  # import over an empty folder: 0 kinds, no crash
        await pilot.pause()
        await pilot.pause()
