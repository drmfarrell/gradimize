"""Shared fixtures for the Gradimize test suite.

`db_manager` used to be copy-pasted into every test module; it lives here
now. Module-local copies still override this one if present — remove them
as modules get touched.
"""

import os
import tempfile
from pathlib import Path

import pytest

# The production server exports GRADIMIZE_DATA_DIR globally, pointing at the
# LIVE data directory. Strip it before src.database.connection is imported
# (it reads the variable at import time) so no test — present or future —
# can ever resolve DEFAULT_DB_PATH to live data.
os.environ.pop("GRADIMIZE_DATA_DIR", None)

from src.database.connection import DatabaseManager  # noqa: E402


@pytest.fixture(autouse=True)
def _runlog_isolation(tmp_path, monkeypatch):
    """Point run logs (src/core/runlog.py) at a per-test temp dir.

    Imports, doctor runs, and solves open run logs implicitly; without this,
    every test run would litter the repo's logs/ directory (and prune real
    run history via the retention cap).
    """
    monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path / "runlogs"))


@pytest.fixture
def db_manager():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield manager

    db_path.unlink(missing_ok=True)
