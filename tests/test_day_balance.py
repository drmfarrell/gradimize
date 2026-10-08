"""Tests for day balance penalty."""

import tempfile
from pathlib import Path

import pytest

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
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
def day_balance_dataset(db_manager):
    """Dataset with MWF and TR slots and multiple courses to spread across days."""
    with db_manager.get_session() as session:
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=3, name="TR_0800", pattern="TR", days=["T", "R"],
                     start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
            TimeSlot(id=4, name="TR_0930", pattern="TR", days=["T", "R"],
                     start_time="09:30", end_time="10:45", duration_minutes=75, slot_type="TR_75"),
        ]
        for s in slots:
            session.add(s)

        rooms = [
            Room(id="R1", building="Main", number="101", capacity=30, room_type=RoomType.LECTURE),
            Room(id="R2", building="Main", number="102", capacity=30, room_type=RoomType.LECTURE),
        ]
        for r in rooms:
            session.add(r)

        # Four courses, so solver can spread them across MWF and TR
        for i in range(1, 5):
            session.add(Course(
                code=f"C{i:03d}", title=f"Course {i}", credits=3,
                structure_type=StructureType.STANDALONE,
                required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"],
            ))

        # Four faculty, one per course
        for i in range(1, 5):
            session.add(Faculty(
                id=f"F{i}", name=f"Prof {i}", email=f"f{i}@test.com",
                max_credits=12, approved_courses=[f"C{i:03d}"],
            ))

        # One student demands all four
        session.add(Student(
            id="S1", name="Student 1", email="s1@test.com",
            class_standing=ClassStanding.FR, credits_earned=0, major="TEST", catalog_year=2024,
        ))
        for i in range(1, 5):
            session.add(StudentDemand(
                student_id="S1", course_code=f"C{i:03d}", urgency_score=10.0,
                is_required=True, is_graduation_critical=False, reason="Test",
            ))

        session.commit()

    return db_manager


def test_day_balance_spreads_sections(day_balance_dataset):
    """Sections are distributed across MWF and TR days with day balance penalty."""
    with day_balance_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.soft_constraints["day_balance_penalty"] = {"weight": -500}
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Day Balance Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Count sections on MWF vs TR slots (via SectionMeeting)
        mwf_count = 0
        tr_count = 0
        for s in result.sections:
            for m in result.meetings:
                if m.section_id == s.id:
                    slot = engine.slot_by_id[m.time_slot_id]
                    if set(slot.days) & {"M", "W", "F"}:
                        mwf_count += 1
                    if set(slot.days) & {"T", "R"}:
                        tr_count += 1

        # With 4 sections and 2 MWF + 2 TR slots, a balanced schedule
        # should have sections on both day patterns
        assert mwf_count > 0, "Should have some MWF sections"
        assert tr_count > 0, "Should have some TR sections"


@pytest.fixture
def single_section_dataset(db_manager):
    """Dataset with a single course/section (edge case)."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(
            id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
            start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50",
        ))
        session.add(Room(
            id="R1", building="Main", number="101", capacity=30, room_type=RoomType.LECTURE,
        ))
        session.add(Course(
            code="C001", title="Only Course", credits=3,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
        ))
        session.add(Faculty(
            id="F1", name="Prof", email="f@test.com",
            max_credits=12, approved_courses=["C001"],
        ))
        session.add(Student(
            id="S1", name="Student", email="s@test.com",
            class_standing=ClassStanding.FR, credits_earned=0, major="TEST", catalog_year=2024,
        ))
        session.add(StudentDemand(
            student_id="S1", course_code="C001", urgency_score=10.0,
            is_required=True, is_graduation_critical=False, reason="Test",
        ))

        session.commit()

    return db_manager


def test_day_balance_no_crash_single_section(single_section_dataset):
    """Day balance penalty doesn't crash with a single section."""
    with single_section_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Single Section Test", "2025SP")

        assert result.status in ("optimal", "feasible")
        assert len(result.sections) > 0
