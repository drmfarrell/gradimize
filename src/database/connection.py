"""Database connection and session management for Gradimize."""

import logging
import os
import threading
from pathlib import Path
from typing import Generator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

# Where the live database lives. By default it sits in the repo's data/
# directory (gitignored), but for real-data deployments set GRADIMIZE_DATA_DIR
# to a path OUTSIDE the git working tree (e.g. /var/lib/gradimize or
# ~/gradimize-data). Keeping student data outside the repo means no git
# operation — not even `git add -A` — can ever capture and push it.
REPO_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_data_dir_env = os.environ.get("GRADIMIZE_DATA_DIR")
DATA_DIR = Path(_data_dir_env).expanduser() if _data_dir_env else REPO_DATA_DIR

# Default database path
DEFAULT_DB_PATH = DATA_DIR / "gradimize.db"

# One engine (and connection pool) per database file for the process
# lifetime. Creating an engine per call defeated pooling, and meant any
# pragma tweak had to be replicated at every call site.
_engines: dict[tuple[str, bool], object] = {}
_engines_lock = threading.Lock()


def _configure_sqlite(engine) -> None:
    """Set per-connection SQLite pragmas.

    - WAL journal: readers (web viewers, TUI) don't block on the solver
      thread's minutes-long write transactions, and vice versa.
    - busy_timeout: a briefly locked database waits instead of instantly
      raising "database is locked".

    PRAGMA foreign_keys stays OFF deliberately: the replace-import flow
    clears whole parent tables (courses, rooms, …) while old schedules
    still reference them, and deletes cascade manually. Enforcing FKs
    needs cascade rules plus an import redesign first (tracked in
    CODE_REVIEW.md).
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def get_engine(db_path: Path | str | None = None, echo: bool = False):
    """Get the process-wide SQLAlchemy engine for a database file.

    Engines are cached per (path, echo): repeated calls — one per request
    in the API — reuse the same engine and its connection pool.

    Args:
        db_path: Path to SQLite database file. If None, uses default.
        echo: If True, log all SQL statements.

    Returns:
        SQLAlchemy engine instance.
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH

    db_path = Path(db_path)
    key = (str(db_path.resolve() if db_path.parent.exists() else db_path), echo)

    with _engines_lock:
        engine = _engines.get(key)
        if engine is None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                f"sqlite:///{db_path}",
                echo=echo,
                # Sessions are per-thread but pooled connections may be
                # handed to different threads; safe with WAL + serialized
                # SQLite, required for the background solver thread.
                connect_args={"check_same_thread": False},
            )
            _configure_sqlite(engine)
            _engines[key] = engine
    return engine


def dispose_engine(db_path: Path | str | None = None) -> None:
    """Drop the cached engine for a path (tests / DB file replacement)."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = Path(db_path)
    with _engines_lock:
        for key in [k for k in _engines if k[0] in (str(db_path), str(db_path.resolve()))]:
            _engines.pop(key).dispose()


def create_tables(engine=None):
    """Create all tables defined in models.

    Args:
        engine: SQLAlchemy engine. If None, uses default.
    """
    if engine is None:
        engine = get_engine()

    # Import all models to ensure they're registered
    from src.database.models import (  # noqa: F401
        Course,
        DegreeRequirement,
        Faculty,
        FinalExamAssignment,
        FinalExamBlock,
        FinalsSchedule,
        FreshmenProjection,
        Prerequisite,
        Room,
        ScheduleVersion,
        Section,
        SectionEnrollment,
        SectionMeeting,
        SemesterGuide,
        SemesterGuideEntry,
        Student,
        StudentCourseHistory,
        StudentDemand,
        TimeSlot,
    )

    SQLModel.metadata.create_all(engine)
    _apply_lightweight_migrations(engine)


def _create_unique_index_if_safe(conn, index_name: str, table: str, columns: list[str]) -> None:
    """Create a UNIQUE index unless existing data already violates it.

    Live databases may already have duplicate (columns) rows from before the
    constraint existed; creating a UNIQUE index over those fails outright.
    Check first and skip (logging a warning) rather than crash the caller.
    """
    cols_sql = ", ".join(columns)
    dupe_count = conn.exec_driver_sql(
        f"SELECT COUNT(*) FROM (SELECT {cols_sql} FROM {table} "
        f"GROUP BY {cols_sql} HAVING COUNT(*) > 1)"
    ).scalar()
    if dupe_count:
        logger.warning(
            "Skipping unique index %s on %s(%s): %d duplicate group(s) found "
            "in existing data. Resolve the duplicates, then restart to apply "
            "the constraint.",
            index_name, table, cols_sql, dupe_count,
        )
        return
    conn.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} ({cols_sql})"
    )


def _apply_lightweight_migrations(engine):
    """Apply additive schema changes that create_all can't pick up.

    SQLAlchemy/SQLModel's create_all only creates missing tables — it won't
    add new columns or indexes to existing ones. For the few additive
    migrations we have, we run idempotent statements here.
    """
    additive_columns = {
        # table -> [(column_name, column_ddl)]
        "section": [
            ("needs_final", "BOOLEAN DEFAULT 1"),
        ],
        "student": [
            ("pacing_preference", "VARCHAR"),
            ("admit_term", "VARCHAR"),
        ],
        "faculty": [
            ("max_overload_credits", "INTEGER"),
        ],
        "course": [
            ("offer_terms", "JSON DEFAULT '[]'"),
            ("cadence", "VARCHAR DEFAULT 'ANNUAL'"),
            ("max_class_size", "INTEGER"),
            ("crosslist_group", "VARCHAR"),
        ],
    }

    # Hot filter columns (kept in sync with index=True in models.py, which
    # only takes effect for freshly created tables).
    indexes = [
        ("ix_section_schedule_id", "section", "schedule_id"),
        ("ix_sectionmeeting_section_id", "sectionmeeting", "section_id"),
        ("ix_sectionenrollment_section_id", "sectionenrollment", "section_id"),
        ("ix_sectionenrollment_student_id", "sectionenrollment", "student_id"),
        ("ix_studentdemand_student_id", "studentdemand", "student_id"),
        ("ix_studentdemand_course_code", "studentdemand", "course_code"),
        ("ix_studentcoursehistory_student_id", "studentcoursehistory", "student_id"),
        ("ix_finalexamblock_finals_schedule_id", "finalexamblock", "finals_schedule_id"),
        (
            "ix_finalexamassignment_finals_schedule_id",
            "finalexamassignment",
            "finals_schedule_id",
        ),
        ("ix_finalsschedule_schedule_id", "finalsschedule", "schedule_id"),
        ("ix_prerequisite_course_code", "prerequisite", "course_code"),
    ]

    with engine.begin() as conn:
        for table, columns in additive_columns.items():
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for col_name, col_ddl in columns:
                if col_name not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}"
                    )

        for name, table, column in indexes:
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})"
            )

        # needs_final used to be nullable; NULL was treated the same as True
        # (finals.py only skips on `is False`). Now that the model declares
        # it as a non-optional bool, backfill any lingering NULLs to match
        # that existing semantics.
        conn.exec_driver_sql(
            "UPDATE section SET needs_final = 1 WHERE needs_final IS NULL"
        )

        # Unique constraints added after these tables were already in use.
        # Guard against pre-existing duplicate data: creating a UNIQUE index
        # over duplicates fails outright, so check first and skip (with a
        # warning) rather than crash the migration.
        for name, table, columns in (
            ("ux_sectionenrollment_section_student", "sectionenrollment",
             ["section_id", "student_id"]),
            ("ux_sectionmeeting_section_sequence", "sectionmeeting",
             ["section_id", "meeting_sequence"]),
        ):
            _create_unique_index_if_safe(conn, name, table, columns)


def get_session(engine=None) -> Generator[Session, None, None]:
    """Yield a database session (FastAPI dependency — shared by all routes).

    Args:
        engine: SQLAlchemy engine. If None, uses default.

    Yields:
        SQLModel session.
    """
    if engine is None:
        engine = get_engine()

    with Session(engine) as session:
        yield session


class DatabaseManager:
    """Context manager for database operations."""

    def __init__(self, db_path: Path | str | None = None, echo: bool = False):
        """Initialize database manager.

        Args:
            db_path: Path to SQLite database file.
            echo: If True, log all SQL statements.
        """
        self.db_path = db_path
        self.echo = echo
        self._engine = None

    @property
    def engine(self):
        """Get or create the database engine."""
        if self._engine is None:
            self._engine = get_engine(self.db_path, self.echo)
        return self._engine

    def create_tables(self):
        """Create all database tables."""
        create_tables(self.engine)

    def get_session(self) -> Session:
        """Get a new database session."""
        return Session(self.engine)

    def reset_database(self):
        """Drop all tables and recreate them (for testing)."""
        SQLModel.metadata.drop_all(self.engine)
        self.create_tables()
