"""Tests for back-to-back penalty and adjacency computation."""

import tempfile
from pathlib import Path

import pytest

from src.core.conflicts import ConflictMatrix, TimeSlotOverlapCalculator
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


# ─────────────────────────────────────────────────────────────────
# Unit tests for adjacency computation
# ─────────────────────────────────────────────────────────────────


def test_adjacency_computation(db_manager):
    """Test compute_adjacencies finds back-to-back slots."""
    with db_manager.get_session() as session:
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=3, name="MWF_1100", pattern="MWF", days=["M", "W", "F"],
                     start_time="11:00", end_time="11:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=4, name="TR_0800", pattern="TR", days=["T", "R"],
                     start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
        ]
        for s in slots:
            session.add(s)
        session.commit()

        calc = TimeSlotOverlapCalculator(session)
        calc.compute_overlaps()
        adj = calc.compute_adjacencies(max_gap_minutes=15)

        # Slot 1 (08:00-08:50) and Slot 2 (09:00-09:50): gap=10min, adjacent
        assert 2 in adj[1], "Slots 1 and 2 should be adjacent (10min gap)"
        assert 1 in adj[2], "Adjacency should be symmetric"

        # Slot 2 (09:00-09:50) and Slot 3 (11:00-11:50): gap=70min, NOT adjacent
        assert 3 not in adj[2], "Slots 2 and 3 should NOT be adjacent (70min gap)"

        # Slot 1 (MWF) and Slot 4 (TR): different days, NOT adjacent
        assert 4 not in adj[1], "Slots on different days should NOT be adjacent"


def test_tight_adjacency_computation(db_manager):
    """Test compute_tight_adjacencies with travel time threshold."""
    with db_manager.get_session() as session:
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0855", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:55", end_time="09:45", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=3, name="MWF_1005", pattern="MWF", days=["M", "W", "F"],
                     start_time="10:05", end_time="10:55", duration_minutes=50, slot_type="MWF_50"),
        ]
        for s in slots:
            session.add(s)
        session.commit()

        calc = TimeSlotOverlapCalculator(session)
        calc.compute_overlaps()
        tight = calc.compute_tight_adjacencies(min_travel_minutes=10)

        # Slot 1 (ends 08:50) -> Slot 2 (starts 08:55): gap=5min < 10min -> tight
        assert 2 in tight[1], "5min gap should be tight-adjacent"

        # Slot 2 (ends 09:45) -> Slot 3 (starts 10:05): gap=20min >= 10min -> NOT tight
        assert 3 not in tight[2], "20min gap should NOT be tight-adjacent"


# ─────────────────────────────────────────────────────────────────
# Integration tests for back-to-back penalty
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def back_to_back_dataset(db_manager):
    """Create a dataset where back-to-back avoidance can be tested.

    Two courses, one faculty, three adjacent slots, two rooms.
    The solver should avoid scheduling the faculty in adjacent slots.
    """
    with db_manager.get_session() as session:
        # Three adjacent MWF slots
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=3, name="TR_0800", pattern="TR", days=["T", "R"],
                     start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
        ]
        for s in slots:
            session.add(s)

        rooms = [
            Room(id="R1", building="Main", number="101", capacity=30, room_type=RoomType.LECTURE),
            Room(id="R2", building="Main", number="102", capacity=30, room_type=RoomType.LECTURE),
        ]
        for r in rooms:
            session.add(r)

        courses = [
            Course(code="C101", title="Course 1", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]),
            Course(code="C102", title="Course 2", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]),
        ]
        for c in courses:
            session.add(c)

        # One faculty teaches both courses
        session.add(Faculty(
            id="F1", name="Prof X", email="x@test.com",
            max_credits=12, approved_courses=["C101", "C102"],
        ))

        session.add(Student(
            id="S1", name="Student 1", email="s1@test.com",
            class_standing=ClassStanding.FR, credits_earned=0, major="TEST", catalog_year=2024,
        ))
        session.add(StudentDemand(
            student_id="S1", course_code="C101", urgency_score=10.0,
            is_required=True, is_graduation_critical=False, reason="Test",
        ))
        session.add(StudentDemand(
            student_id="S1", course_code="C102", urgency_score=10.0,
            is_required=True, is_graduation_critical=False, reason="Test",
        ))

        session.commit()

    return db_manager


def test_faculty_back_to_back_penalized(back_to_back_dataset):
    """Faculty avoids adjacent slots when back-to-back penalty is strong."""
    with back_to_back_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.soft_constraints["back_to_back_penalty"] = {
            "faculty": {"max_consecutive": 2, "weight": -5000},
            "student": {"max_consecutive": 3, "weight": -20},
        }
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "B2B Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Faculty F1 should not have adjacent slots
        f1_slots = []
        for s in result.sections:
            if s.faculty_id == "F1":
                for m in result.meetings:
                    if m.section_id == s.id:
                        f1_slots.append(m.time_slot_id)

        # With a strong penalty and an alternative TR slot available,
        # the solver should spread the sections across non-adjacent slots
        for s1 in f1_slots:
            for s2 in f1_slots:
                if s1 != s2:
                    assert s2 not in engine.slot_adjacencies.get(s1, set()), \
                        f"Faculty should avoid adjacent slots {s1} and {s2}"


def test_faculty_back_to_back_opt_out(back_to_back_dataset):
    """allow_back_to_back=True disables the penalty for that faculty."""
    with back_to_back_dataset.get_session() as session:
        # Set allow_back_to_back for faculty F1
        faculty = session.get(Faculty, "F1")
        faculty.preferences = {"allow_back_to_back": True}
        session.add(faculty)
        session.commit()

        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.soft_constraints["back_to_back_penalty"] = {
            "faculty": {"max_consecutive": 2, "weight": -5000},
            "student": {"max_consecutive": 3, "weight": -20},
        }
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "B2B OptOut Test", "2025SP")

        assert result.status in ("optimal", "feasible")
        # With opt-out, solving should succeed without errors.
        # The faculty can be in adjacent slots since the penalty is disabled.
        assert len(result.sections) > 0
