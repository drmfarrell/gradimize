"""Solver-closable sections: run at minimum enrollment or close and consolidate."""

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


def _seed(session, *, n_students=6, min_enrollment=5, grad_critical=False,
          extra_course=False):
    for i in (1, 2):
        session.add(TimeSlot(id=i, name=f"MWF_{8 + i:02d}00", pattern="MWF",
                             days=["M", "W", "F"], start_time=f"{8 + i:02d}:00",
                             end_time=f"{8 + i:02d}:50", duration_minutes=50,
                             slot_type="MWF_50"))
    for r in ("R1", "R2"):
        session.add(Room(id=r, building="B", number=r[-1], capacity=30,
                         room_type=RoomType.LECTURE))
    session.add(Course(code="CSCI101", title="Intro", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                       min_enrollment=min_enrollment))
    if extra_course:
        session.add(Course(code="GHOST101", title="No Demand", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"], min_enrollment=min_enrollment))
    session.add(Faculty(id="F1", name="Prof", email="p@t.co", max_credits=12,
                        approved_courses=[]))
    for i in range(n_students):
        session.add(Student(id=f"S{i + 1}", name=f"Student {i + 1}", email="",
                            class_standing=ClassStanding.SO, credits_earned=30,
                            major="CSCI", catalog_year=2024))
        session.add(StudentDemand(student_id=f"S{i + 1}", course_code="CSCI101",
                                  urgency_score=20.0, is_required=True,
                                  is_graduation_critical=grad_critical, reason="t"))
    session.commit()


def _engine(session, closure=True):
    cm = ConflictMatrix(session)
    cm.build_matrix()
    config = SchedulerConfig()
    config.solver_settings["max_time_seconds"] = 30
    config.section_settings["allow_section_closure"] = closure
    return SchedulingEngine(session, cm, config)


def test_closure_off_keeps_forced_sections(db_manager):
    """Default behavior unchanged: both forced sections exist."""
    with db_manager.get_session() as session:
        _seed(session, n_students=6, min_enrollment=5)
        engine = _engine(session, closure=False)
        sections = engine.create_sections("2026FA",
                                          sections_per_course={"CSCI101": 2})
        result = engine.solve(sections, "Off Test", "2026FA")
        assert result.status in ("optimal", "feasible")
        assert len(result.sections) == 2
        assert result.stats["sections_closed"] == 0


def test_solver_consolidates_below_min_sibling(db_manager):
    """6 students, min 5, two forced sections: one must close, all 6 seated."""
    with db_manager.get_session() as session:
        _seed(session, n_students=6, min_enrollment=5)
        engine = _engine(session, closure=True)
        sections = engine.create_sections("2026FA",
                                          sections_per_course={"CSCI101": 2})
        result = engine.solve(sections, "Consolidate Test", "2026FA")
        assert result.status in ("optimal", "feasible")
        assert len(result.sections) == 1, "second section can't reach min=5"
        assert len(result.enrollments) == 6, "closure must not strand anyone"
        assert result.stats["sections_closed"] == 1


def test_below_min_demand_still_served(db_manager):
    """Only 3 students exist (min 5): the section opens with all 3 anyway."""
    with db_manager.get_session() as session:
        _seed(session, n_students=3, min_enrollment=5)
        engine = _engine(session, closure=True)
        sections = engine.create_sections("2026FA")
        result = engine.solve(sections, "Below Min Test", "2026FA")
        assert result.status in ("optimal", "feasible")
        assert len(result.sections) == 1
        assert len(result.enrollments) == 3, (
            "min_enrollment must never strand students when demand is short"
        )


def test_grad_critical_below_min_protected(db_manager):
    """Grad-critical demand below the minimum still gets seated."""
    with db_manager.get_session() as session:
        _seed(session, n_students=2, min_enrollment=5, grad_critical=True)
        engine = _engine(session, closure=True)
        sections = engine.create_sections("2026FA")
        result = engine.solve(sections, "Grad Critical Test", "2026FA")
        assert result.status in ("optimal", "feasible")
        assert len(result.enrollments) == 2


def test_zero_demand_section_closes(db_manager):
    """A course nobody needs doesn't squat on a room."""
    with db_manager.get_session() as session:
        _seed(session, n_students=6, min_enrollment=5, extra_course=True)
        engine = _engine(session, closure=True)
        sections = engine.create_sections("2026FA")
        assert any(s.course_code == "GHOST101" for s in sections)
        result = engine.solve(sections, "Ghost Test", "2026FA")
        assert result.status in ("optimal", "feasible")
        assert not any(s.course_code == "GHOST101" for s in result.sections)


def test_crosslist_pack_closes_together(db_manager):
    """Cross-listed packs open/close as one class."""
    with db_manager.get_session() as session:
        for i in (1, 2):
            session.add(TimeSlot(id=i, name=f"MWF_{8 + i:02d}00", pattern="MWF",
                                 days=["M", "W", "F"], start_time=f"{8 + i:02d}:00",
                                 end_time=f"{8 + i:02d}:50", duration_minutes=50,
                                 slot_type="MWF_50"))
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        for code in ("HIST201", "AFAM201"):
            session.add(Course(code=code, title="AAH", credits=3,
                               structure_type=StructureType.STANDALONE,
                               required_room_types=["LECTURE"],
                               valid_slot_types=["MWF_50"], min_enrollment=5,
                               crosslist_group="XL1"))
        session.add(Faculty(id="F1", name="Prof", email="p@t.co", max_credits=12,
                            approved_courses=[]))
        # 6 pooled students, all under HIST201 (AFAM side carries none).
        for i in range(6):
            session.add(Student(id=f"S{i + 1}", name=f"S{i + 1}", email="",
                                class_standing=ClassStanding.SO, credits_earned=30,
                                major="HIST", catalog_year=2024))
            session.add(StudentDemand(student_id=f"S{i + 1}",
                                      course_code="HIST201", urgency_score=20.0,
                                      is_required=True,
                                      is_graduation_critical=False, reason="t"))
        session.commit()

        engine = _engine(session, closure=True)
        sections = engine.create_sections(
            "2026FA", sections_per_course={"HIST201": 2, "AFAM201": 2}
        )
        result = engine.solve(sections, "XL Closure Test", "2026FA")
        assert result.status in ("optimal", "feasible")
        by_code = {}
        for s in result.sections:
            by_code.setdefault(s.course_code, []).append(s)
        # Pack #2 (6 pooled < 2x5) closes on BOTH codes; pack #1 survives on
        # both — the empty AFAM mirror stays open with its leader.
        assert len(by_code.get("HIST201", [])) == 1
        assert len(by_code.get("AFAM201", [])) == 1
        assert len(result.enrollments) == 6
