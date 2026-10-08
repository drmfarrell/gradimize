"""Throwaway SQLite copies of the live database for what-if runs.

The what-if runners (``scenarios``, ``stress``, ``profiles``, ``longrange``,
``replan``) all re-solve against a DISPOSABLE copy of the database so the live
data is never touched. This module is the one home for making and disposing of
those copies.

**Never ``shutil.copy`` a SQLite database.** The live DB runs in WAL mode, so
uncommitted writes live in a ``-wal`` sidecar; a plain file copy silently drops
them and yields a subtly-stale scratch DB. The SQLite online-backup API
(``sqlite3.Connection.backup``) copies a transactionally-consistent snapshot,
sidecar included — always use it (via :func:`backup_db` / :func:`scratch_copy`).
"""

import sqlite3
import tempfile
from pathlib import Path


def backup_db(source: Path | str, dest: Path | str) -> None:
    """Online-backup ``source`` into ``dest`` (both SQLite files).

    Reads ``source`` only; writes a consistent snapshot to ``dest``. Use this
    when the caller manages the destination path itself; otherwise prefer
    :func:`scratch_copy`.
    """
    with sqlite3.connect(source) as src_conn, sqlite3.connect(dest) as dst_conn:
        src_conn.backup(dst_conn)


def scratch_copy(source: Path | str, label: str = "scratch") -> Path:
    """Make a fresh temp-file copy of ``source`` and return its path.

    The file is created under the system temp dir as
    ``gradimize_<label>_XXXX.db`` and is NOT auto-deleted — the caller owns it
    (pass it to :func:`delete_scratch` when done, or keep it for inspection).
    """
    fd = tempfile.NamedTemporaryFile(
        prefix=f"gradimize_{label}_", suffix=".db", delete=False
    )
    scratch_path = Path(fd.name)
    fd.close()
    backup_db(source, scratch_path)
    return scratch_path


def delete_scratch(path: Path | str) -> None:
    """Remove a scratch DB and its WAL/SHM sidecars. Safe if already gone."""
    path = Path(path)
    path.unlink(missing_ok=True)
    Path(str(path) + "-wal").unlink(missing_ok=True)
    Path(str(path) + "-shm").unlink(missing_ok=True)
