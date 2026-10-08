"""Tests for the historical-teaching derivation importer."""

import csv
import tempfile
from pathlib import Path

import pytest

from src.core.teaching_history import (
    NARROWING_WARNING,
    derive_teaching_history,
)
from src.database.connection import DatabaseManager
from src.database.models import Faculty, TimeSlot

SCHEDULE_HEADERS = [
    "TERM", "CRN", "SUBJECT", "COURSE_NUMBER", "SECTION", "CAPACITY",
    "ACTUAL_ENROLLMENT", "INSTRUCTOR_ID", "MEETING_DAYS", "START_TIME",
    "END_TIME", "BUILDING", "ROOM", "XLST_GROUP", "INSTRUCTIONAL_METHOD",
]


def _write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SCHEDULE_HEADERS)
        w.writerows(rows)
    return path


@pytest.fixture
def db_manager():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield manager
    db_path.unlink(missing_ok=True)


@pytest.fixture
def seeded_db(db_manager):
    """DB with TimeSlots + a few Faculty rows (some with pre-existing data)."""
    with db_manager.get_session() as session:
        session.add_all([
            TimeSlot(
                id=1, name="MWF_0930", pattern="MWF", days=["M", "W", "F"],
                start_time="09:30", end_time="10:20", duration_minutes=50,
                slot_type="MWF_50",
            ),
            TimeSlot(
                id=2, name="TR_1000", pattern="TR", days=["T", "R"],
                start_time="10:00", end_time="11:15", duration_minutes=75,
                slot_type="TR_75",
            ),
            TimeSlot(
                id=3, name="MWF_1130", pattern="MWF", days=["M", "W", "F"],
                start_time="11:30", end_time="12:20", duration_minutes=50,
                slot_type="MWF_50",
            ),
        ])
        session.add(Faculty(id="F00231", name="Prof Bio", email="bio@u.edu"))
        session.add(Faculty(id="F00198", name="Prof Chem", email="chem@u.edu"))
        # Already curated: department has already set approved_courses.
        session.add(Faculty(
            id="F00442", name="Prof Hist", email="hist@u.edu",
            approved_courses=["HIST210"],
        ))
        session.commit()
    return db_manager


@pytest.fixture
def csv_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    _write_csv(d / "schedule_202610.csv", [
        ["202610", "20451", "BIOL", "201", "001", "30", "28", "F00231",
         "MWF", "09:30", "10:20", "SCI", "104", "", "F2F"],
        ["202610", "20452", "BIOL", "201L", "001", "24", "22", "F00231",
         "MWF", "09:30", "10:20", "SCI", "201", "", "F2F"],
        ["202610", "20771", "CHEM", "101", "002", "35", "30", "F00198",
         "TR", "10:00", "11:15", "SCI", "150", "", "F2F"],
        ["202610", "20815", "HIST", "210", "001", "30", "25", "F00442",
         "MWF", "11:30", "12:20", "HALL", "102", "XL07", "F2F"],
        ["202610", "20999", "MATH", "150", "001", "20", "18", "F09999",
         "MWF", "08:00", "08:50", "SCI", "300", "", "F2F"],
        # blank instructor -> skipped
        ["202610", "21100", "ART", "100", "001", "20", "5", "",
         "MWF", "08:00", "08:50", "ART", "1", "", "F2F"],
    ])
    _write_csv(d / "schedule_202620.csv", [
        ["202620", "30451", "BIOL", "201", "001", "30", "20", "F00231",
         "MWF", "09:30", "10:20", "SCI", "104", "", "F2F"],
        ["202620", "30452", "BIOL", "202", "001", "30", "20", "F00231",
         "TR", "10:00", "11:15", "SCI", "104", "", "F2F"],
        # online section for F00198: counts toward courses, not patterns
        ["202620", "30771", "CHEM", "150", "090", "40", "38", "F00198",
         "", "", "", "", "", "", "OL"],
    ])
    return d


def test_fill_only_default(seeded_db, csv_dir):
    with seeded_db.get_session() as session:
        stats = derive_teaching_history(session, csv_dir)

    assert stats["files"] == 2
    assert stats["faculty_updated"] >= 2
    assert stats["faculty_skipped_nonempty"] == 1  # F00442 already curated
    assert stats["unknown_instructor_ids"] == ["F09999"]

    with seeded_db.get_session() as session:
        bio = session.get(Faculty, "F00231")
        chem = session.get(Faculty, "F00198")
        hist = session.get(Faculty, "F00442")

        assert bio.approved_courses == ["BIOL201", "BIOL201L", "BIOL202"]
        assert chem.approved_courses == ["CHEM101", "CHEM150"]
        # Untouched: fill-only, already had a curated list.
        assert hist.approved_courses == ["HIST210"]


def test_multi_term_aggregation_and_preferred_slots(seeded_db, csv_dir):
    with seeded_db.get_session() as session:
        derive_teaching_history(session, csv_dir)

    with seeded_db.get_session() as session:
        bio = session.get(Faculty, "F00231")
        # BIOL201 appears in both terms.
        assert bio.preferences["derived_history"]["courses"]["BIOL201"] == 2
        assert bio.preferences["derived_history"]["courses"]["BIOL201L"] == 1
        assert bio.preferences["derived_history"]["courses"]["BIOL202"] == 1
        # Dominant meeting pattern: MWF 09:30 occurs 3x (2 terms of BIOL201 +
        # BIOL201L), TR 10:00 occurs once -> preferred_slots is engine format
        # (a list of TimeSlot ids the solver's preference bonus reads).
        assert bio.preferences["preferred_slots"] == [1]


def test_engine_reads_preferred_slots_format(seeded_db, csv_dir):
    """Prove the emitted key/shape matches what penalties.py consumes."""
    with seeded_db.get_session() as session:
        derive_teaching_history(session, csv_dir)
        chem = session.get(Faculty, "F00198")
        preferred = set(chem.preferences.get("preferred_slots", []))
        assert preferred  # non-empty, matches engine's .get() access pattern
        for slot_id in preferred:
            assert isinstance(slot_id, int)
            slot = session.get(TimeSlot, slot_id)
            assert slot is not None


def test_overwrite_replaces_existing(seeded_db, csv_dir):
    with seeded_db.get_session() as session:
        derive_teaching_history(session, csv_dir, overwrite=True)

    with seeded_db.get_session() as session:
        hist = session.get(Faculty, "F00442")
        # Overwrite replaces even a pre-existing curated list.
        assert hist.approved_courses == ["HIST210"]  # only course in the data
        assert "preferred_slots" in hist.preferences


def test_overwrite_vs_fill_only_on_preferences(seeded_db, csv_dir):
    # Pre-set a preferred_slots value before deriving; fill-only must not touch it.
    with seeded_db.get_session() as session:
        fac = session.get(Faculty, "F00231")
        fac.preferences = {"preferred_slots": [999]}
        session.add(fac)
        session.commit()

    with seeded_db.get_session() as session:
        derive_teaching_history(session, csv_dir)
    with seeded_db.get_session() as session:
        assert session.get(Faculty, "F00231").preferences["preferred_slots"] == [999]

    with seeded_db.get_session() as session:
        derive_teaching_history(session, csv_dir, overwrite=True)
    with seeded_db.get_session() as session:
        assert session.get(Faculty, "F00231").preferences["preferred_slots"] == [1]


def test_dry_run_commits_nothing(seeded_db, csv_dir):
    with seeded_db.get_session() as session:
        stats = derive_teaching_history(session, csv_dir, dry_run=True)
    assert stats["dry_run"] is True

    with seeded_db.get_session() as session:
        bio = session.get(Faculty, "F00231")
        assert bio.approved_courses == []
        assert bio.preferences == {}


def test_narrowing_warning_present_in_report(seeded_db, csv_dir):
    lines = []
    with seeded_db.get_session() as session:
        derive_teaching_history(session, csv_dir, emit=lines.append)
    assert any(NARROWING_WARNING == line for line in lines)
    assert "can teach anything" in NARROWING_WARNING
    assert "NARROWS" in NARROWING_WARNING


def test_unknown_instructor_never_autocreated(seeded_db, csv_dir):
    with seeded_db.get_session() as session:
        stats = derive_teaching_history(session, csv_dir)
    assert "F09999" in stats["unknown_instructor_ids"]
    with seeded_db.get_session() as session:
        from sqlmodel import select
        assert session.exec(
            select(Faculty).where(Faculty.id == "F09999")
        ).first() is None
