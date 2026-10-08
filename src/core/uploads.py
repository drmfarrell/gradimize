"""Upload-driven data import for the web GUI.

Accepts a single uploaded file for a given "kind" (rooms, courses, faculty,
students, …), writes it to a temp directory under the canonical filename the
DataIngestor expects, optionally clears existing rows of that kind, then runs
the matching loader.

Import is REPLACE-by-default: re-uploading a file wipes the existing rows of
that kind and reloads from scratch, so the operation is idempotent. (SQLite
foreign keys are not enforced by default, so clearing a table that other rows
reference will not cascade — callers importing into a DB that already has
schedules should be aware they can orphan references.)
"""

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import delete as sa_delete
from sqlmodel import Session

from src.core.ingest import DataIngestor
from src.core.runlog import RunLog
from src.database.models import (
    Course,
    DegreeRequirement,
    Faculty,
    FreshmenProjection,
    Prerequisite,
    Room,
    SemesterGuide,
    SemesterGuideEntry,
    Student,
    StudentCourseHistory,
    TimeSlot,
)

logger = logging.getLogger(__name__)


@dataclass
class ImportKind:
    """Describes one importable file kind."""

    key: str
    label: str
    filename: str  # canonical filename the DataIngestor expects
    description: str
    # Models whose rows are deleted before (re)loading this kind.
    clears: list[type] = field(default_factory=list)
    is_yaml: bool = False


# Order here is also the recommended upload order (dependencies first).
IMPORT_KINDS: list[ImportKind] = [
    ImportKind(
        "time_slots", "Time Slots", "time_slots.yaml",
        "YAML defining available meeting blocks (MWF 8:30, TR 10:00, labs…).",
        clears=[TimeSlot], is_yaml=True,
    ),
    ImportKind(
        "rooms", "Rooms", "rooms.csv",
        "Physical rooms: id, building, number, capacity, room_type, equipment.",
        clears=[Room],
    ),
    ImportKind(
        "courses", "Course Catalog", "catalog.csv",
        "Catalog: code, title, credits, structure_type, room/slot types, "
        "offer_terms, cadence.",
        clears=[Course],
    ),
    ImportKind(
        "prerequisites", "Prerequisites", "prerequisites.csv",
        "course_code, prereq_code, min_grade, is_corequisite.",
        clears=[Prerequisite],
    ),
    ImportKind(
        "faculty", "Faculty", "faculty.csv",
        "Instructors: id, name, email, max_credits, approved_courses.",
        clears=[Faculty],
    ),
    ImportKind(
        "students", "Students", "students.csv",
        "Roster: id, name, class_standing, major, credits_earned, catalog_year.",
        clears=[Student],
    ),
    ImportKind(
        "course_history", "Course History", "course_history.csv",
        "Past enrollments used for demand/prereq satisfaction.",
        clears=[StudentCourseHistory],
    ),
    ImportKind(
        "degree_requirements", "Degree Requirements", "degree_requirements.csv",
        "Major requirements driving internal demand calculation.",
        clears=[DegreeRequirement],
    ),
    ImportKind(
        "freshmen_projections", "Freshmen Projections", "freshmen_projections.csv",
        "Projected incoming-class course demand.",
        clears=[FreshmenProjection],
    ),
    ImportKind(
        "meeting_patterns", "Meeting Patterns", "meeting_patterns.csv",
        "Per-course multi-meeting patterns (updates existing courses).",
        clears=[],  # updates Course rows in place; nothing to clear
    ),
    ImportKind(
        "registrations", "Registrations (→ demand)", "registrations.csv",
        "Active registrations; becomes observed StudentDemand (replaces "
        "existing non-projected demands).",
        clears=[],  # the importer clears non-projected demands itself, atomically
    ),
    ImportKind(
        "semester_guide", "Semester Guide", "semester_guide.csv",
        "Per-major semester-by-semester course plan.",
        clears=[SemesterGuide, SemesterGuideEntry],
    ),
]

IMPORT_KINDS_BY_KEY: dict[str, ImportKind] = {k.key: k for k in IMPORT_KINDS}

# Kinds whose loaders validate rows WITHOUT consulting existing DB rows.
# These get a dry-run parse on a scratch database before the real replace:
# the loaders commit internally, so once the real load runs, a file whose
# rows ALL fail validation would land the wipe with nothing to replace it.
# (prerequisites/freshmen_projections/meeting_patterns/semester_guide check
# existing courses, so a scratch dry-run would wrongly reject valid files.)
DRY_RUN_KINDS = {
    "rooms", "courses", "faculty", "students",
    "course_history", "degree_requirements",
}

# Kinds whose files carry FERPA-protected student rows. Exceptions raised
# while importing these can embed cell values in their messages, so the run
# log records type + frames only (include_msg=False). See src/core/runlog.py.
STUDENT_KINDS = {"students", "course_history", "registrations"}


@dataclass
class FileImportResult:
    kind: str
    filename: str
    rows_loaded: int
    cleared: bool
    errors: list[str] = field(default_factory=list)


def _loader_for(ingestor: DataIngestor, kind: ImportKind, tmp_dir: Path) -> Callable[[], int]:
    """Return a zero-arg callable that runs the right loader and returns a count."""
    if kind.key == "time_slots":
        path = tmp_dir / kind.filename
        return lambda: len(ingestor.load_time_slots(path))
    mapping: dict[str, Callable[[], int]] = {
        "rooms": lambda: len(ingestor.load_rooms()),
        "courses": lambda: len(ingestor.load_courses()),
        "prerequisites": lambda: len(ingestor.load_prerequisites()),
        "faculty": lambda: len(ingestor.load_faculty()),
        "students": lambda: len(ingestor.load_students()),
        "course_history": lambda: len(ingestor.load_course_history()),
        "degree_requirements": lambda: len(ingestor.load_degree_requirements()),
        "freshmen_projections": lambda: len(ingestor.load_freshmen_projections()),
        "meeting_patterns": lambda: ingestor.load_meeting_patterns(),
        "semester_guide": lambda: len(ingestor.load_semester_guides()),
        "registrations": lambda: _import_registrations(ingestor, tmp_dir),
    }
    return mapping[kind.key]


def _import_registrations(ingestor: DataIngestor, tmp_dir: Path) -> int:
    from src.core.registrations import import_registrations_as_demand

    stats = import_registrations_as_demand(
        ingestor.session, tmp_dir / "registrations.csv",
        runlog=ingestor.runlog,
    )
    ingestor.errors.extend(stats.get("errors", []))
    return stats["demands_created"]


def import_data_file(
    session: Session,
    kind_key: str,
    content: bytes,
    replace: bool = True,
) -> FileImportResult:
    """Import a single uploaded data file.

    Args:
        session: DB session.
        kind_key: One of IMPORT_KINDS_BY_KEY.
        content: Raw uploaded file bytes.
        replace: If True (default), delete existing rows of this kind first.

    Returns:
        FileImportResult with rows_loaded and any validation errors.

    Raises:
        ValueError: If kind_key is unknown.
    """
    kind = IMPORT_KINDS_BY_KEY.get(kind_key)
    if kind is None:
        raise ValueError(f"Unknown import kind: {kind_key!r}")

    # Every import leaves a FERPA-safe record on disk (src/core/runlog.py):
    # what was imported, what failed, and value *shapes* for bad student
    # rows — debuggable without ever reading the data itself.
    runlog = RunLog("import")
    runlog.event("import_start", kind=kind_key, filename=kind.filename,
                 bytes=len(content), replace=replace)
    safe_messages = kind.key not in STUDENT_KINDS
    try:
        # Dry-run first for self-contained kinds: parse into a throwaway
        # in-memory DB. A well-formed file whose rows ALL fail validation
        # would otherwise commit the wipe below with zero replacement rows
        # (the loaders commit internally, so post-hoc rollback cannot undo it).
        if replace and kind.clears and kind.key in DRY_RUN_KINDS:
            from sqlmodel import SQLModel, create_engine

            scratch = create_engine("sqlite://")
            SQLModel.metadata.create_all(scratch)
            with tempfile.TemporaryDirectory() as td:
                tmp_dir = Path(td)
                (tmp_dir / kind.filename).write_bytes(content)
                with Session(scratch) as probe_session:
                    probe = DataIngestor(probe_session, tmp_dir, runlog=runlog)
                    try:
                        probe_rows = _loader_for(probe, kind, tmp_dir)()
                    except Exception as e:
                        runlog.exception("dry_run", e, include_msg=safe_messages)
                        runlog.event("import_aborted", kind=kind_key,
                                     reason="dry_run_exception")
                        return FileImportResult(
                            kind=kind_key, filename=kind.filename, rows_loaded=0,
                            cleared=False, errors=[f"{type(e).__name__}: {e}"],
                        )
            if probe_rows == 0 and probe.errors:
                runlog.event("import_aborted", kind=kind_key,
                             reason="no_valid_rows", n_errors=len(probe.errors))
                return FileImportResult(
                    kind=kind_key, filename=kind.filename, rows_loaded=0,
                    cleared=False,
                    errors=probe.errors
                    + ["No valid rows in the file — existing data left untouched."],
                )

        # Clear-then-load runs in ONE transaction: the deletes below are NOT
        # committed here — the loader's own commit lands the wipe and the reload
        # together, and the rollback in the except restores the old rows if the
        # uploaded file turns out to be unparseable.
        cleared = False
        if replace and kind.clears:
            for model in kind.clears:
                session.execute(sa_delete(model))

        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            (tmp_dir / kind.filename).write_bytes(content)
            ingestor = DataIngestor(session, tmp_dir, runlog=runlog)
            try:
                rows = _loader_for(ingestor, kind, tmp_dir)()
                cleared = bool(replace and kind.clears)
            except Exception as e:  # surface loader/commit failures as errors, don't 500
                session.rollback()
                logger.exception("Import failed for kind %s", kind_key)
                runlog.exception("import", e, include_msg=safe_messages)
                runlog.event("import_failed", kind=kind_key)
                return FileImportResult(
                    kind=kind_key, filename=kind.filename, rows_loaded=0,
                    cleared=False, errors=[f"{type(e).__name__}: {e}"],
                )

        runlog.event("import_done", kind=kind_key, rows_loaded=rows,
                     cleared=cleared, n_errors=len(ingestor.errors))
        return FileImportResult(
            kind=kind_key,
            filename=kind.filename,
            rows_loaded=rows,
            cleared=cleared,
            errors=list(ingestor.errors),
        )
    finally:
        runlog.close()
