"""Coverage for src/core/logsetup.py — the app-log channel bug reports use."""

import logging
import sys

import pytest

from src.core import logsetup


@pytest.fixture(autouse=True)
def _fresh_logsetup(monkeypatch):
    """Undo the process-global init so each test configures from scratch."""
    monkeypatch.setattr(logsetup, "_initialized_path", None)
    prev_hook = sys.excepthook
    yield
    sys.excepthook = prev_hook
    for name in ("src", "src.crash", "src.test_logsetup_probe"):
        logging.getLogger(name).handlers.clear()


def test_init_creates_file_and_captures_module_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path))
    logfile = logsetup.init_logging(app="script")
    assert logfile == tmp_path / "gradimize.log"

    logging.getLogger("src.test_logsetup_probe").warning("probe-message-xyz")
    assert "probe-message-xyz" in logfile.read_text()


def test_init_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path))
    first = logsetup.init_logging()
    monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path / "elsewhere"))
    assert logsetup.init_logging() == first


def test_tui_mode_has_no_console_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path))
    logsetup.init_logging(app="tui")
    handlers = logging.getLogger("src").handlers
    assert handlers, "src logger should have the file handler"
    assert not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
        for h in handlers
    ), "TUI mode must not log to stdout (corrupts the Textual screen)"


def test_excepthook_writes_traceback_to_log(tmp_path, monkeypatch):
    monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path))
    logfile = logsetup.init_logging(app="script")

    try:
        raise ValueError("boom-for-logsetup-test")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    text = logfile.read_text()
    assert "boom-for-logsetup-test" in text
    assert "Traceback" in text


def test_broken_yaml_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logsetup, "_CONFIG_PATH", tmp_path / "missing.yaml")
    logfile = logsetup.init_logging(app="script")
    logging.getLogger("src.test_logsetup_probe").error("fallback-probe")
    assert "fallback-probe" in logfile.read_text()
