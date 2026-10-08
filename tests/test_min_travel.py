"""Tests for minimum travel time hard constraint."""

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
def travel_dataset(db_manager):
    """Dataset with tight-adjacent slots and rooms in different buildings.

    Two MWF slots with a 5-minute gap (tight-adjacent with 10min threshold).
    Two rooms in different buildings.
    Two courses taught by the same faculty.
    """
    with db_manager.get_session() as session:
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0855", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:55", end_time="09:45", duration_minutes=50, slot_type="MWF_50"),
            # A third slot with sufficient gap from slot 1
            TimeSlot(id=3, name="MWF_1000", pattern="MWF", days=["M", "W", "F"],
                     start_time="10:00", end_time="10:50", duration_minutes=50, slot_type="MWF_50"),
            # TR slot as an alternative
            TimeSlot(id=4, name="TR_0800", pattern="TR", days=["T", "R"],
                     start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
        ]
        for s in slots:
            session.add(s)

        rooms = [
            Room(
                id="R_SCI_101", building="Science", number="101",
                capacity=30, room_type=RoomType.LECTURE,
            ),
            Room(
                id="R_ART_201", building="Arts", number="201",
                capacity=30, room_type=RoomType.LECTURE,
            ),
            Room(
                id="R_SCI_102", building="Science", number="102",
                capacity=30, room_type=RoomType.LECTURE,
            ),
        ]
        for r in rooms:
            session.add(r)

        courses = [
            Course(code="PHYS101", title="Physics", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]),
            Course(code="ART101", title="Art History", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]),
        ]
        for c in courses:
            session.add(c)

        # One faculty teaches both, forcing the travel constraint to matter
        session.add(Faculty(
            id="F1", name="Prof Travel", email="travel@test.com",
            max_credits=12, approved_courses=["PHYS101", "ART101"],
        ))

        session.add(Student(
            id="S1", name="Student 1", email="s1@test.com",
            class_standing=ClassStanding.FR, credits_earned=0, major="TEST", catalog_year=2024,
        ))
        session.add(StudentDemand(
            student_id="S1", course_code="PHYS101", urgency_score=10.0,
            is_required=True, is_graduation_critical=False, reason="Test",
        ))
        session.add(StudentDemand(
            student_id="S1", course_code="ART101", urgency_score=10.0,
            is_required=True, is_graduation_critical=False, reason="Test",
        ))

        session.commit()

    return db_manager


def test_faculty_different_building_tight_adjacent_forbidden(travel_dataset):
    """Faculty can't teach in different buildings with too-short gap."""
    with travel_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.hard_constraints["min_travel_time"] = 10
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Travel Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Find F1's assignments via SectionMeeting
        f1_assignments = []
        for s in result.sections:
            if s.faculty_id == "F1":
                for m in result.meetings:
                    if m.section_id == s.id:
                        f1_assignments.append((m.time_slot_id, m.room_id))

        # If F1 teaches in tight-adjacent slots, they must be in the same building
        for (slot1, room1), (slot2, room2) in zip(f1_assignments, f1_assignments[1:]):
            if slot2 in engine.slot_tight_adjacencies.get(slot1, set()):
                bldg1 = engine.room_by_id[room1].building
                bldg2 = engine.room_by_id[room2].building
                assert bldg1 == bldg2, \
                    f"Faculty in different buildings ({bldg1}, {bldg2}) with tight gap"


def test_same_building_tight_adjacent_allowed(travel_dataset):
    """Same building is fine even with tight gap."""
    with travel_dataset.get_session() as session:
        # Remove the Arts building room so both rooms are in Science
        art_room = session.get(Room, "R_ART_201")
        if art_room:
            session.delete(art_room)
            session.commit()

        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.hard_constraints["min_travel_time"] = 10
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Same Building Test", "2025SP")

        assert result.status in ("optimal", "feasible")
        # All assignments are in Science building, so tight adjacency is fine
        for m in result.meetings:
            assert engine.room_by_id[m.room_id].building == "Science"


def test_sufficient_gap_different_buildings_allowed(travel_dataset):
    """Adequate gap between slots means no travel restriction."""
    with travel_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.hard_constraints["min_travel_time"] = 10
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)

        # Slots 1 (08:00-08:50) and 3 (10:00-10:50) have a 70min gap
        # They should NOT be tight-adjacent
        assert 3 not in engine.slot_tight_adjacencies.get(1, set()), \
            "70min gap should not be tight-adjacent"

        # Solve should succeed and can place sections in slots 1 and 3
        # in different buildings
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Sufficient Gap Test", "2025SP")

        assert result.status in ("optimal", "feasible")
