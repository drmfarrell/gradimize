"""Tests for the ingest-hardening fixes: class_standing/structure_type/credits
fallbacks, case-insensitive Banner headers, XLST_GROUP backfill, and
admit_term end-to-end netting against freshmen projections."""

import csv
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.demand import ProjectionDemandCalculator
from src.core.graph import PrerequisiteGraph
from src.core.ingest import DataIngestor
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    FreshmenProjection,
    Room,
    RoomType,
    Section,
    SectionMeeting,
    StructureType,
    Student,
    TimeSlot,
)


@pytest.fixture
def db_manager():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield manager

    db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────
# FIX 1: class_standing fallback
# ─────────────────────────────────────────────────────────────────


def test_missing_class_standing_column_derives_from_credits(db_manager, tmp_path):
    """No class_standing column at all -> derive_standing() thresholds apply."""
    (tmp_path / "students.csv").write_text(
        "id,credits_earned,major,catalog_year\n"
        "S001,0,CSCI,2024\n"
        "S002,100,CSCI,2024\n"
    )
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, tmp_path)
        students = ingestor.load_students()

        assert len(students) == 2
        s1 = session.get(Student, "S001")
        s2 = session.get(Student, "S002")
        assert s1.class_standing == ClassStanding.FR
        assert s2.class_standing == ClassStanding.SR
        assert ingestor.errors == []


def test_blank_class_standing_cell_derives(db_manager, tmp_path):
    """class_standing column present but blank on a row -> also derived."""
    (tmp_path / "students.csv").write_text(
        "id,class_standing,credits_earned,major,catalog_year\n"
        "S001,,45,CSCI,2024\n"
    )
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, tmp_path)
        students = ingestor.load_students()

        assert len(students) == 1
        s1 = session.get(Student, "S001")
        assert s1.class_standing == ClassStanding.SO
        assert ingestor.errors == []


def test_invalid_class_standing_still_errors_and_skips(db_manager, tmp_path):
    """A present but unrecognized value is still an error, row skipped."""
    (tmp_path / "students.csv").write_text(
        "id,class_standing,credits_earned,major,catalog_year\n"
        "S001,GR,45,CSCI,2024\n"
        "S002,FR,10,CSCI,2024\n"
    )
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, tmp_path)
        students = ingestor.load_students()

        assert len(students) == 1
        assert students[0].id == "S002"
        assert len(ingestor.errors) == 1
        assert "Invalid class standing" in ingestor.errors[0]


# ─────────────────────────────────────────────────────────────────
# FIX 2: structure_type crash guard
# ─────────────────────────────────────────────────────────────────


def test_missing_structure_type_column_clean_error(db_manager, tmp_path):
    """Absent structure_type column -> one clean error, no KeyError crash."""
    (tmp_path / "catalog.csv").write_text(
        "code,title,credits\n"
        "TEST101,Test Course,3\n"
    )
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, tmp_path)
        courses = ingestor.load_courses()

        assert courses == []
        assert len(ingestor.errors) == 1
        assert "missing structure_type" in ingestor.errors[0]


# ─────────────────────────────────────────────────────────────────
# FIX 3: credits crash guard
# ─────────────────────────────────────────────────────────────────


def test_bad_credits_cell_errors_and_skips_other_rows_load(db_manager, tmp_path):
    (tmp_path / "catalog.csv").write_text(
        "code,title,credits,structure_type\n"
        "TEST101,Test Course,notanumber,STANDALONE\n"
        "TEST102,Second Course,3,STANDALONE\n"
    )
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, tmp_path)
        courses = ingestor.load_courses()

        assert len(courses) == 1
        assert courses[0].code == "TEST102"
        assert any("Invalid credits" in e for e in ingestor.errors)


# ─────────────────────────────────────────────────────────────────
# FIX 4/5: Banner schedule import — case-insensitive headers, XLST_GROUP
# ─────────────────────────────────────────────────────────────────


def _make_lower_csv(*rows):
    header = [
        "crn", "subject", "course_number", "section", "credits",
        "meeting_days", "start_time", "end_time", "building", "room",
        "capacity", "instructor_id", "xlst_group",
    ]
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(h, "") for h in header])
    return output.getvalue()


@pytest.fixture
def banner_ready_db(db_manager):
    with db_manager.get_session() as session:
        session.add(TimeSlot(
            id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
            start_time="08:00", end_time="08:50", duration_minutes=50,
            slot_type="MWF_50",
        ))
        session.add(Room(
            id="SCI-101", building="Science", number="101", capacity=30,
            room_type=RoomType.LECTURE,
        ))
        session.add(Faculty(
            id="FSMITH01", name="Dr. Smith", email="smith@test.edu",
            max_credits=12, approved_courses=["CSCI1301"],
        ))
        session.add(Course(
            code="CSCI1301", title="Intro to CS", credits=3,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
        ))
        session.commit()
    return db_manager


def test_lowercase_header_banner_csv_imports(banner_ready_db):
    csv_content = _make_lower_csv({
        "crn": "10001", "subject": "CSCI", "course_number": "1301",
        "section": "001", "credits": "3", "meeting_days": "MWF",
        "start_time": "08:00", "end_time": "08:50",
        "building": "Science", "room": "101", "capacity": "30",
        "instructor_id": "FSMITH01",
    })
    with banner_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert len(result.errors) == 0
        assert result.sections_imported == 1
        assert result.meetings_imported == 1

        section = session.exec(select(Section).where(Section.crn == "10001")).first()
        assert section is not None
        meetings = list(session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id == section.id)
        ).all())
        assert len(meetings) == 1


def test_xlst_group_backfills_and_counts(banner_ready_db):
    csv_content = _make_lower_csv({
        "crn": "10002", "subject": "CSCI", "course_number": "1301",
        "section": "002", "credits": "3", "meeting_days": "MWF",
        "start_time": "08:00", "end_time": "08:50",
        "building": "Science", "room": "101", "capacity": "30",
        "instructor_id": "FSMITH01", "xlst_group": "XL07",
    })
    with banner_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.crosslist_backfilled == 1
        course = session.get(Course, "CSCI1301")
        assert course.crosslist_group == "XL07"


def test_xlst_group_conflict_warns_without_overwrite(banner_ready_db):
    with banner_ready_db.get_session() as session:
        course = session.get(Course, "CSCI1301")
        course.crosslist_group = "XL01"
        session.add(course)
        session.commit()

    csv_content = _make_lower_csv({
        "crn": "10003", "subject": "CSCI", "course_number": "1301",
        "section": "003", "credits": "3", "meeting_days": "MWF",
        "start_time": "08:00", "end_time": "08:50",
        "building": "Science", "room": "101", "capacity": "30",
        "instructor_id": "FSMITH01", "xlst_group": "XL07",
    })
    with banner_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.crosslist_backfilled == 0
        assert any("conflicts with existing crosslist group" in w for w in result.warnings)
        course = session.get(Course, "CSCI1301")
        assert course.crosslist_group == "XL01"


# ─────────────────────────────────────────────────────────────────
# FIX 6: admit_term round-trip and demand netting
# ─────────────────────────────────────────────────────────────────


def test_admit_term_roundtrips_through_import(db_manager, tmp_path):
    (tmp_path / "students.csv").write_text(
        "id,class_standing,credits_earned,major,catalog_year,admit_term\n"
        "S001,FR,10,CSCI,2024,2026FA\n"
        "S002,FR,10,CSCI,2024,\n"
    )
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, tmp_path)
        ingestor.load_students()

        s1 = session.get(Student, "S001")
        s2 = session.get(Student, "S002")
        assert s1.admit_term == "2026FA"
        assert s2.admit_term is None


def test_projection_nets_real_admits(db_manager):
    with db_manager.get_session() as session:
        session.add(Course(
            code="CSCI101", title="Intro to CS", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))
        for i in range(3):
            session.add(Student(
                id=f"REAL{i}", name=f"Real {i}", email=f"real{i}@test.edu",
                class_standing=ClassStanding.FR, credits_earned=0,
                major="CSCI", catalog_year=2024, admit_term="2026FA",
            ))
        session.add(FreshmenProjection(
            major="CSCI", estimated_count=10, catalog_year=2026, is_active=True,
        ))
        session.commit()

        graph = PrerequisiteGraph(session)
        calc = ProjectionDemandCalculator(session, graph)
        result = calc.generate_projected_demand()

        assert result["students_created"] == 7
        assert result["netted_real_admits"] == 3


def test_projection_netting_noop_without_admit_term(db_manager):
    """No admit_term anywhere -> identical to pre-fix behavior."""
    with db_manager.get_session() as session:
        session.add(Course(
            code="CSCI101", title="Intro to CS", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))
        session.add(Student(
            id="S00001", name="Real Student", email="real@example.com",
            class_standing=ClassStanding.SO, credits_earned=30,
            major="CSCI", catalog_year=2024,
        ))
        session.add(FreshmenProjection(
            major="CSCI", estimated_count=5, catalog_year=2024, is_active=True,
        ))
        session.commit()

        graph = PrerequisiteGraph(session)
        calc = ProjectionDemandCalculator(session, graph)
        result = calc.generate_projected_demand()

        assert result["students_created"] == 5
        assert result["netted_real_admits"] == 0
