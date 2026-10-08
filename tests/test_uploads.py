"""Tests for upload-driven data import (src/core/uploads.py)."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.uploads import import_data_file
from src.database.connection import DatabaseManager
from src.database.models import Room, RoomType


@pytest.fixture
def db_manager():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield manager

    db_path.unlink(missing_ok=True)


GOOD_ROOMS_CSV = b"""id,building,number,capacity,room_type,equipment
SCI101,Science Hall,101,40,LECTURE,projector
SCI201,Science Hall,201,24,LAB_WET,"fume hood, sinks"
"""

# capacity is non-numeric -> loader raises mid-parse
BAD_ROOMS_CSV = b"""id,building,number,capacity,room_type,equipment
SCI101,Science Hall,101,forty,LECTURE,projector
"""


def _seed_room(session):
    session.add(
        Room(
            id="OLD1", building="Old Hall", number="1", capacity=10,
            room_type=RoomType.LECTURE, equipment=[], is_active=True,
        )
    )
    session.commit()


def test_replace_import_success(db_manager):
    with db_manager.get_session() as session:
        _seed_room(session)

        result = import_data_file(session, "rooms", GOOD_ROOMS_CSV, replace=True)

        assert result.rows_loaded == 2
        assert result.cleared is True
        assert result.errors == []

        ids = {r.id for r in session.exec(select(Room)).all()}
        assert ids == {"SCI101", "SCI201"}


def test_failed_replace_import_preserves_existing_rows(db_manager):
    """Regression: a malformed upload must NOT wipe the existing table.

    The clear used to be committed before the loader parsed the file, so a
    bad CSV left the table empty. Clear + load now share one transaction.
    """
    with db_manager.get_session() as session:
        _seed_room(session)

        result = import_data_file(session, "rooms", BAD_ROOMS_CSV, replace=True)

        assert result.rows_loaded == 0
        assert result.cleared is False
        assert result.errors  # surfaced, not raised

        ids = {r.id for r in session.exec(select(Room)).all()}
        assert ids == {"OLD1"}, "existing rows must survive a failed import"


def test_rooms_import_without_optional_columns(db_manager):
    """Regression: missing optional 'equipment' column used to KeyError."""
    csv_no_equipment = b"""id,building,number,capacity,room_type
SCI101,Science Hall,101,40,LECTURE
"""
    with db_manager.get_session() as session:
        result = import_data_file(session, "rooms", csv_no_equipment, replace=True)

        assert result.errors == []
        assert result.rows_loaded == 1
        room = session.exec(select(Room)).one()
        assert room.equipment == []
        assert room.is_active is True


def test_rooms_import_parses_string_booleans(db_manager):
    """Regression: is_active='false' used to be truthy via bool('false')."""
    csv_inactive = b"""id,building,number,capacity,room_type,is_active
SCI101,Science Hall,101,40,LECTURE,false
SCI102,Science Hall,102,40,LECTURE,true
"""
    with db_manager.get_session() as session:
        result = import_data_file(session, "rooms", csv_inactive, replace=True)

        assert result.errors == []
        by_id = {r.id: r for r in session.exec(select(Room)).all()}
        assert by_id["SCI101"].is_active is False
        assert by_id["SCI102"].is_active is True


def test_unknown_kind_raises(db_manager):
    with db_manager.get_session() as session:
        with pytest.raises(ValueError):
            import_data_file(session, "nonsense", b"", replace=True)


def test_students_import_never_stores_identifiers(db_manager):
    """DATA_HANDLING_PROCEDURES §2: even a RAW (non-deidentified) extract delivered
    by mistake must not land a student name/email/DOB in the database."""
    from src.database.models import Student

    raw_csv = (b"id,name,email,phone_number,birth_date,class_standing,"
               b"credits_earned,major,catalog_year\n"
               b"900123456,Jane Doe,jdoe@school.edu,555-0100,2004-03-02,"
               b"SR,98,BIOL,2023\n")
    with db_manager.get_session() as session:
        result = import_data_file(session, "students", raw_csv, replace=True)

        assert result.errors == []
        assert result.rows_loaded == 1
        st = session.exec(select(Student)).one()
        assert st.name == "Student 900123456", "name column must be dropped"
        assert st.email == ""
        for leaked in ("Jane", "Doe", "jdoe", "555-0100", "2004-03-02"):
            assert leaked not in f"{st!r}", f"{leaked!r} stored on the model"


def test_all_rows_invalid_replace_preserves_existing(db_manager):
    """A well-formed file whose rows ALL fail validation must not commit the
    wipe (the loaders commit internally, so this is caught by a dry run)."""
    from src.database.models import ClassStanding, Student

    with db_manager.get_session() as session:
        session.add(Student(id="KEEP1", name="", email="", major="BIOL",
                            class_standing=ClassStanding.SR, credits_earned=90,
                            catalog_year=2023))
        session.commit()

        bad_standing = (b"id,class_standing,credits_earned,major,catalog_year\n"
                        b"S1,WIZARD,10,BIOL,2023\n"
                        b"S2,SORCERER,20,CHEM,2024\n")
        result = import_data_file(session, "students", bad_standing, replace=True)

        assert result.rows_loaded == 0
        assert result.cleared is False
        assert result.errors, "row failures must be surfaced"
        ids = {s.id for s in session.exec(select(Student)).all()}
        assert ids == {"KEEP1"}, "existing rows must survive an all-invalid file"


def test_semester_guide_load_commits_once(db_manager, tmp_path, monkeypatch):
    """Regression: load_semester_guides must commit ONCE for the whole load,
    not once per guide. Per-guide commits landed the caller's wipe (and the
    guides built so far) before a later failure could roll it back, breaking
    uploads.py's clear-then-load-atomically contract for this kind.
    """
    from src.core.ingest import DataIngestor
    from src.database.models import Course, SemesterGuideEntry, StructureType

    (tmp_path / "semester_guide.csv").write_text(
        "major,concentration,catalog_year,semester_number,"
        "course_code,category,credits,label\n"
        "BIOL,,2023,1,BIO101,major,3,Intro Bio\n"
        "CHEM,,2023,1,CHM101,major,3,Intro Chem\n"
    )

    with db_manager.get_session() as session:
        session.add(Course(code="BIO101", title="B", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Course(code="CHM101", title="C", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.commit()

        real_commit = session.commit
        commits = 0

        def counting_commit():
            nonlocal commits
            commits += 1
            return real_commit()

        monkeypatch.setattr(session, "commit", counting_commit)

        guides = DataIngestor(session, tmp_path).load_semester_guides()

        assert len(guides) == 2, "both guide groups created"
        assert commits == 1, "two guide groups must still be a single commit"
        # flush (not commit) assigned guide.id, so entries linked correctly
        entries = session.exec(select(SemesterGuideEntry)).all()
        assert {e.course_code for e in entries} == {"BIO101", "CHM101"}


def test_registrations_import_creates_observed_demand(db_manager):
    from sqlmodel import select as sel

    from src.database.models import (
        ClassStanding,
        Course,
        DegreeRequirement,
        StructureType,
        Student,
        StudentDemand,
    )

    with db_manager.get_session() as session:
        session.add(Course(code="BIO101", title="B", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Student(id="S1", name="", email="", major="BIOL",
                            class_standing=ClassStanding.SR, credits_earned=100,
                            catalog_year=2023))
        session.add(DegreeRequirement(major="BIOL", catalog_year=2023,
                                      course_code="BIO101", is_required=True,
                                      category="major"))
        # stale predicted demand that must be replaced
        session.add(StudentDemand(student_id="S1", course_code="BIO101",
                                  urgency_score=1.0, is_required=False, reason="old"))
        session.commit()

        csv_bytes = (b"student_id,term,crn,subject,course_number,section\n"
                     b"S1,202710,10001,BIO,101,001\n"
                     b"S1,202710,10001,BIO,101,001\n"      # duplicate row
                     b"GHOST,202710,10002,BIO,101,001\n")  # unknown student
        result = import_data_file(session, "registrations", csv_bytes)

        assert result.rows_loaded == 1
        assert any("GHOST" in e for e in result.errors)
        demands = session.exec(sel(StudentDemand)).all()
        assert len(demands) == 1, "old predicted demand replaced, no dupes"
        d = demands[0]
        assert d.is_graduation_critical is True  # SR + required
        assert "Registered (observed demand)" in d.reason
