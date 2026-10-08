"""Tests for faculty preferred teaching slots and unavailable slots."""

import tempfile
from pathlib import Path

import pytest

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.core.ingest import DataIngestor
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
    StructureType,
    Student,
    StudentDemand,
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


@pytest.fixture
def preference_dataset(db_manager):
    """Create a dataset for faculty preference tests.

    Single faculty member teaching a single course with 3 available slots.
    """
    with db_manager.get_session() as session:
        # Three time slots
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=3, name="MWF_1000", pattern="MWF", days=["M", "W", "F"],
                     start_time="10:00", end_time="10:50", duration_minutes=50, slot_type="MWF_50"),
        ]
        for s in slots:
            session.add(s)

        # One room
        session.add(
            Room(id="R1", building="Test", number="101", capacity=30, room_type=RoomType.LECTURE)
        )

        # One course
        session.add(
            Course(code="CSCI101", title="Intro CS", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50"])
        )

        # Faculty — no unavailable / no preferences by default
        session.add(
            Faculty(id="F1", name="Prof A", email="a@test.com",
                    max_credits=12, approved_courses=["CSCI101"])
        )

        # Student with demand
        session.add(
            Student(id="S1", name="Student 1", email="s1@test.com",
                    class_standing=ClassStanding.SO, credits_earned=30,
                    major="CSCI", catalog_year=2024)
        )
        session.add(
            StudentDemand(student_id="S1", course_code="CSCI101",
                          urgency_score=20.0, is_required=True,
                          is_graduation_critical=False, reason="Test")
        )

        session.commit()

    return db_manager


# ─────────────────────────────────────────────────────────────────
# 1. Unavailable slot hard constraint
# ─────────────────────────────────────────────────────────────────


def test_unavailable_slot_respected(preference_dataset):
    """Faculty with unavailable_slots=[1] is never assigned to slot 1."""
    # Set slot 1 as unavailable for F1
    with preference_dataset.get_session() as session:
        faculty = session.get(Faculty, "F1")
        faculty.unavailable_slots = [1]
        session.add(faculty)
        session.commit()

    with preference_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        assert result.status in ("optimal", "feasible")
        assert len(result.sections) > 0

        for section in result.sections:
            if section.faculty_id == "F1":
                for meeting in result.meetings:
                    if meeting.section_id == section.id:
                        assert meeting.time_slot_id != 1, \
                            "Faculty meeting placed in unavailable slot 1"


# ─────────────────────────────────────────────────────────────────
# 2. Preferred slot soft constraint
# ─────────────────────────────────────────────────────────────────


def test_preferred_slot_chosen(preference_dataset):
    """Faculty with preferred slots should be assigned to one when possible."""
    # Prefer slot 3 only
    with preference_dataset.get_session() as session:
        faculty = session.get(Faculty, "F1")
        faculty.preferences = {"preferred_slots": [3]}
        session.add(faculty)
        session.commit()

    with preference_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        assert result.status in ("optimal", "feasible")
        assert len(result.sections) > 0

        assigned_slots = [
            m.time_slot_id for s in result.sections if s.faculty_id == "F1"
            for m in result.meetings if m.section_id == s.id
        ]
        assert 3 in assigned_slots, \
            f"Faculty not assigned to preferred slot 3, got slots: {assigned_slots}"


# ─────────────────────────────────────────────────────────────────
# 3. Preference does not override hard constraint
# ─────────────────────────────────────────────────────────────────


def test_preference_does_not_override_hard_constraints(preference_dataset):
    """A preferred slot that is also unavailable is still forbidden."""
    with preference_dataset.get_session() as session:
        faculty = session.get(Faculty, "F1")
        faculty.unavailable_slots = [3]
        faculty.preferences = {"preferred_slots": [3]}
        session.add(faculty)
        session.commit()

    with preference_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        assert result.status in ("optimal", "feasible")
        assert len(result.sections) > 0

        for section in result.sections:
            if section.faculty_id == "F1":
                for meeting in result.meetings:
                    if meeting.section_id == section.id:
                        assert meeting.time_slot_id != 3, \
                            "Faculty assigned to slot 3 even though it is unavailable"


# ─────────────────────────────────────────────────────────────────
# 4. Preferences JSON ingestion
# ─────────────────────────────────────────────────────────────────


def test_preferences_json_ingestion(db_manager, tmp_path):
    """CSV with JSON preferences column loads correctly."""
    # Minimal required CSV files
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        "code,title,credits,structure_type,linked_course_code,required_room_types,valid_slot_types,min_enrollment\n"
        "TEST101,Test,3,STANDALONE,,LECTURE,MWF_50,5\n"
    )
    rooms = tmp_path / "rooms.csv"
    rooms.write_text(
        "id,building,number,capacity,room_type,equipment,is_active\n"
        "R1,Test,101,30,LECTURE,projector,True\n"
    )
    students = tmp_path / "students.csv"
    students.write_text(
        "id,name,email,class_standing,credits_earned,major,minor,is_honors,is_athlete,catalog_year\n"
    )

    # Faculty CSV with JSON preferences
    faculty_csv = tmp_path / "faculty.csv"
    faculty_csv.write_text(
        'id,name,email,max_credits,approved_courses,unavailable_slots,preferences\n'
        'F001,Dr. Test,test@u.edu,12,TEST101,"1,2","{""preferred_slots"": [3, 5]}"\n'
        'F002,Dr. Empty,empty@u.edu,12,TEST101,,\n'
    )

    # Other required CSV files
    time_slots = tmp_path / "time_slots.csv"
    time_slots.write_text(
        "id,name,pattern,days,start_time,end_time,duration_minutes,slot_type\n"
    )
    prereqs = tmp_path / "prerequisites.csv"
    prereqs.write_text("course_code,prereq_code,min_grade,is_corequisite\n")
    history = tmp_path / "course_history.csv"
    history.write_text("student_id,course_code,grade,semester,credits_earned\n")
    requirements = tmp_path / "degree_requirements.csv"
    requirements.write_text("major,course_code,is_required,category,catalog_year\n")

    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, tmp_path)
        ingestor.load_courses()
        ingestor.load_rooms()
        faculty_list = ingestor.load_faculty()

        assert len(faculty_list) == 2

        f1 = next(f for f in faculty_list if f.id == "F001")
        assert f1.unavailable_slots == [1, 2]
        assert f1.preferences == {"preferred_slots": [3, 5]}

        f2 = next(f for f in faculty_list if f.id == "F002")
        assert f2.preferences == {}


# ─────────────────────────────────────────────────────────────────
# 5. Empty preferences — no effect
# ─────────────────────────────────────────────────────────────────


def test_empty_preferences_no_effect(preference_dataset):
    """Faculty with empty preferences still gets assigned normally."""
    with preference_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        assert result.status in ("optimal", "feasible")
        assert len(result.sections) > 0

        # Faculty should be assigned somewhere
        assigned = [s for s in result.sections if s.faculty_id == "F1"]
        assert len(assigned) > 0, "Faculty F1 was not assigned to any section"


# ─────────────────────────────────────────────────────────────────
# 6. API PATCH endpoint
# ─────────────────────────────────────────────────────────────────


def test_api_update_preferences(db_manager):
    """PATCH endpoint updates preferences and returns correctly."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes.data import get_session, router

    # Seed data
    with db_manager.get_session() as session:
        session.add(
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50,
                     slot_type="MWF_50")
        )
        session.add(
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50,
                     slot_type="MWF_50")
        )
        session.add(
            Faculty(id="F1", name="Prof A", email="a@test.com",
                    max_credits=12, approved_courses=["CSCI101"])
        )
        session.commit()

    # Create test app
    test_app = FastAPI()
    test_app.include_router(router)

    def override_session():
        with db_manager.get_session() as session:
            yield session

    test_app.dependency_overrides[get_session] = override_session

    client = TestClient(test_app)

    # Update unavailable_slots and preferred_slots
    resp = client.patch("/data/faculty/F1/preferences", json={
        "unavailable_slots": [1],
        "preferred_slots": [2],
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "F1"
    assert data["unavailable_slots"] == [1]
    assert data["preferences"]["preferred_slots"] == [2]

    # Verify GET returns updated data
    resp = client.get("/data/faculty/F1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unavailable_slots"] == [1]
    assert data["preferences"]["preferred_slots"] == [2]

    # Invalid slot ID returns 400
    resp = client.patch("/data/faculty/F1/preferences", json={
        "unavailable_slots": [999],
    })
    assert resp.status_code == 400

    # Non-existent faculty returns 404
    resp = client.patch("/data/faculty/NONE/preferences", json={
        "preferred_slots": [1],
    })
    assert resp.status_code == 404
