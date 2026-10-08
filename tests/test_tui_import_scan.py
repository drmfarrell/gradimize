"""Tests for scan_import_folder, the pure folder-scanning helper behind the
TUI's folder-watch import screen."""

from pathlib import Path

from src.tui.screens.import_screen import scan_import_folder


def test_missing_folder_all_absent():
    rows, unrecognized = scan_import_folder(Path("/nonexistent/does-not-exist-xyz"))
    assert unrecognized == []
    assert len(rows) > 0
    assert all(row["exists"] is False for row in rows)
    assert all(row["size"] is None and row["mtime"] is None for row in rows)


def test_found_files_marked_present(tmp_path):
    (tmp_path / "students.csv").write_text("id,name\n1,Alice\n")
    (tmp_path / "rooms.csv").write_text("id,building\n1,Hall\n")

    rows, unrecognized = scan_import_folder(tmp_path)
    by_filename = {row["filename"]: row for row in rows}

    assert by_filename["students.csv"]["exists"] is True
    assert by_filename["students.csv"]["size"] > 0
    assert by_filename["students.csv"]["mtime"] is not None

    assert by_filename["rooms.csv"]["exists"] is True
    assert unrecognized == []


def test_missing_files_marked_absent_in_existing_folder(tmp_path):
    (tmp_path / "students.csv").write_text("id,name\n1,Alice\n")
    rows, _ = scan_import_folder(tmp_path)
    by_filename = {row["filename"]: row for row in rows}
    assert by_filename["rooms.csv"]["exists"] is False


def test_unrecognized_files_listed_sorted(tmp_path):
    (tmp_path / "zeta.csv").write_text("x")
    (tmp_path / "alpha.txt").write_text("x")
    (tmp_path / "students.csv").write_text("id,name\n1,Alice\n")

    _, unrecognized = scan_import_folder(tmp_path)
    assert unrecognized == ["alpha.txt", "zeta.csv"]


def test_dotfiles_and_subdirs_ignored(tmp_path):
    (tmp_path / ".hidden.csv").write_text("x")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.csv").write_text("x")

    _, unrecognized = scan_import_folder(tmp_path)
    assert unrecognized == []


def test_rows_are_in_import_kinds_order(tmp_path):
    from src.core.uploads import IMPORT_KINDS

    rows, _ = scan_import_folder(tmp_path)
    assert [row["key"] for row in rows] == [k.key for k in IMPORT_KINDS]
