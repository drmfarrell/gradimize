"""Faculty quality penalties: fewer preps, fewer teaching days."""

import tempfile
from pathlib import Path

import pytest

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.database.connection import DatabaseManager
from src.database.models import (
    Course,
    Faculty,
    Room,
    RoomType,
    StructureType,
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


def _config(prep_w=0, day_w=0):
    config = SchedulerConfig()
    config.solver_settings["max_time_seconds"] = 30
    config.soft_constraints["faculty_prep_penalty"] = {"weight": prep_w}
    config.soft_constraints["faculty_teaching_day_penalty"] = {"weight": day_w}
    return config


def test_prep_penalty_clusters_courses_per_instructor(db_manager):
    """Two instructors, two 2-section courses: heavy prep penalty makes each
    instructor take both sections of ONE course instead of a mix."""
    with db_manager.get_session() as session:
        for i in range(1, 5):
            session.add(TimeSlot(id=i, name=f"MWF_{7 + i:02d}00", pattern="MWF",
                                 days=["M", "W", "F"], start_time=f"{7 + i:02d}:00",
                                 end_time=f"{7 + i:02d}:50", duration_minutes=50,
                                 slot_type="MWF_50"))
        for r in ("R1", "R2"):
            session.add(Room(id=r, building="B", number=r[-1], capacity=30,
                             room_type=RoomType.LECTURE))
        for code in ("AAA101", "BBB101"):
            session.add(Course(code=code, title=code, credits=3,
                               structure_type=StructureType.STANDALONE,
                               required_room_types=["LECTURE"],
                               valid_slot_types=["MWF_50"]))
        for fid in ("F1", "F2"):
            session.add(Faculty(id=fid, name=fid, email=f"{fid}@t.co",
                                max_credits=12,
                                approved_courses=["AAA101", "BBB101"]))
        session.commit()

        cm = ConflictMatrix(session)
        cm.build_matrix()
        engine = SchedulingEngine(session, cm, _config(prep_w=-100))
        sections = engine.create_sections(
            "2026FA", sections_per_course={"AAA101": 2, "BBB101": 2}
        )
        result = engine.solve(sections, "Prep Test", "2026FA")
        assert result.status in ("optimal", "feasible")

        courses_per_faculty = {}
        for s in result.sections:
            courses_per_faculty.setdefault(s.faculty_id, set()).add(s.course_code)
        assert all(len(cs) == 1 for cs in courses_per_faculty.values()), (
            f"each instructor should carry ONE distinct course, "
            f"got {courses_per_faculty}"
        )


def test_day_penalty_compacts_teaching_days(db_manager):
    """Single-day slots on Monday and Tuesday: with a heavy day penalty the
    instructor's two sections land on the SAME day."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="M_0900", pattern="M", days=["M"],
                             start_time="09:00", end_time="09:50",
                             duration_minutes=50, slot_type="MWF_50"))
        session.add(TimeSlot(id=2, name="M_1000", pattern="M", days=["M"],
                             start_time="10:00", end_time="10:50",
                             duration_minutes=50, slot_type="MWF_50"))
        session.add(TimeSlot(id=3, name="T_0900", pattern="T", days=["T"],
                             start_time="09:00", end_time="09:50",
                             duration_minutes=50, slot_type="MWF_50"))
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Course(code="AAA101", title="A", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="F1", email="f@t.co", max_credits=12,
                            approved_courses=["AAA101"]))
        session.commit()

        cm = ConflictMatrix(session)
        cm.build_matrix()
        engine = SchedulingEngine(session, cm, _config(day_w=-100))
        sections = engine.create_sections(
            "2026FA", sections_per_course={"AAA101": 2}
        )
        result = engine.solve(sections, "Day Test", "2026FA")
        assert result.status in ("optimal", "feasible")

        slot_ids = {m.time_slot_id for m in result.meetings}
        assert slot_ids == {1, 2}, (
            f"both sections should stack on Monday (slots 1+2), got {slot_ids}"
        )


def test_zero_weights_build_nothing(db_manager):
    """Weights of 0 skip the machinery entirely (no vars, no terms)."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="M_0900", pattern="M", days=["M"],
                             start_time="09:00", end_time="09:50",
                             duration_minutes=50, slot_type="MWF_50"))
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Course(code="AAA101", title="A", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="F1", email="f@t.co", max_credits=12,
                            approved_courses=["AAA101"]))
        session.commit()

        cm = ConflictMatrix(session)
        cm.build_matrix()
        engine = SchedulingEngine(session, cm, _config(prep_w=0, day_w=0))
        sections = engine.create_sections("2026FA")
        terms = engine._add_faculty_quality_penalties(
            __import__("ortools.sat.python.cp_model", fromlist=["CpModel"]).CpModel(),
            sections, {}, {},
        )
        assert terms == []
