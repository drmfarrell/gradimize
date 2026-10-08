"""Application-wide file logging — the channel bug reports are built from.

Loads ``config/logging.yaml`` and points its file handler at
``runlog_dir()/gradimize.log`` (same resolution as the FERPA-safe run logs:
$GRADIMIZE_LOG_DIR else repo ``logs/``, never the data zone). Also installs a
``sys.excepthook`` so an unhandled crash lands in the log file with its full
traceback instead of dying only in terminal scrollback.

Log-content rule (matches runlog.py): messages may carry course codes, room
and slot names, CRNs, faculty ids, counts, timings, and exception text —
never an individual student's id together with their attributes.
"""

import logging
import logging.config
import logging.handlers
import sys
from pathlib import Path

import yaml

from src.core.runlog import runlog_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "logging.yaml"
_initialized_path: Path | None = None


def log_file_path() -> Path:
    """Where the application log lives (whether or not it exists yet)."""
    return runlog_dir() / "gradimize.log"


def init_logging(app: str = "script") -> Path:
    """Configure logging once per process; return the log file path.

    ``app`` selects console behavior: the TUI must not write to stdout
    (it would corrupt the Textual screen), so ``"tui"`` drops the console
    handler and logs to file only. ``"api"``/``"script"`` keep console
    output alongside the file.
    """
    global _initialized_path
    if _initialized_path is not None:
        return _initialized_path

    logfile = log_file_path()
    logfile.parent.mkdir(parents=True, exist_ok=True)

    try:
        config = yaml.safe_load(_CONFIG_PATH.read_text())
        config["handlers"]["file"]["filename"] = str(logfile)
        if app == "tui":
            for logger_cfg in config.get("loggers", {}).values():
                handlers = logger_cfg.get("handlers", [])
                logger_cfg["handlers"] = [h for h in handlers if h != "console"]
            root_handlers = config.get("root", {}).get("handlers", [])
            config["root"]["handlers"] = [h for h in root_handlers if h != "console"]
        logging.config.dictConfig(config)
    except Exception:  # broken/missing yaml must never take the app down
        handler = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
        ))
        src_logger = logging.getLogger("src")
        src_logger.setLevel(logging.DEBUG)
        src_logger.addHandler(handler)
        if app != "tui":
            src_logger.addHandler(logging.StreamHandler(sys.stdout))
        logging.getLogger(__name__).exception(
            "config/logging.yaml unusable — fell back to built-in log config"
        )

    _install_excepthook()
    _initialized_path = logfile
    return logfile


def _install_excepthook() -> None:
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logging.getLogger("src.crash").critical(
                "Unhandled exception — include %s in the bug report "
                "(./bugreport.sh collects it automatically)",
                log_file_path(), exc_info=(exc_type, exc, tb),
            )
        previous(exc_type, exc, tb)

    sys.excepthook = hook
