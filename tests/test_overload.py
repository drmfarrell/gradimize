"""Overload allowances and loud unstaffable-course failures."""

import tempfile
from pathlib import Path

import pytest

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.core.ingest import DataIngestor
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


def _seed(session, *, faculty_overload=None, approved=("AAA101",)):
    for i in (1, 2):
        session.add(TimeSlot(id=i, name=f"MWF_{8 + i:02d}00", pattern="MWF",
                             days=["M", "W", "F"], start_time=f"{8 + i:02d}:00",
                             end_time=f"{8 + i:02d}:50", duration_minutes=50,
                             slot_type="MWF_50"))
    session.add(Room(id="R1", building="B", number="1", capacity=30,
                     room_type=RoomType.LECTURE))
    session.add(Course(code="AAA101", title="A", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
    session.add(Faculty(id="F1", name="F1", email="f@t.co", max_credits=3,
                        max_overload_credits=faculty_overload,
                        approved_courses=list(approved)))
    session.commit()


def _engine(session, *, load_limit=True, blanket_overload=0):
    cm = ConflictMatrix(session)
    cm.build_matrix()
    config = SchedulerConfig()
    config.solver_settings["max_time_seconds"] = 20
    config.hard_constraints["faculty_load_limit"] = load_limit
    config.hard_constraints["default_overload_credits"] = blanket_overload
    return SchedulingEngine(session, cm, config)


def test_load_limit_without_overload_is_infeasible(db_manager):
    """Two 3-credit sections, one instructor capped at 3 credits: no overload
    means the second section cannot be staffed."""
    with db_manager.get_session() as session:
        _seed(session)
        engine = _engine(session, blanket_overload=0)
        sections = engine.create_sections("2026FA",
                                          sections_per_course={"AAA101": 2})
        result = engine.solve(sections, "No Overload", "2026FA")
        assert result.status == "infeasible"
        assert any("overload" in w.lower() for w in result.staffing_warnings), (
            "the pre-flight must state the overload shortfall"
        )


def test_blanket_overload_unlocks_the_second_section(db_manager):
    with db_manager.get_session() as session:
        _seed(session)
        engine = _engine(session, blanket_overload=3)
        sections = engine.create_sections("2026FA",
                                          sections_per_course={"AAA101": 2})
        result = engine.solve(sections, "Blanket Overload", "2026FA")
        assert result.status in ("optimal", "feasible")
        assert all(s.faculty_id == "F1" for s in result.sections)
        assert result.staffing_warnings == []


def test_per_person_overload_overrides_blanket(db_manager):
    """An explicit max_overload_credits=0 beats a generous blanket."""
    with db_manager.get_session() as session:
        _seed(session, faculty_overload=0)
        engine = _engine(session, blanket_overload=6)
        sections = engine.create_sections("2026FA",
                                          sections_per_course={"AAA101": 2})
        result = engine.solve(sections, "Override Test", "2026FA")
        assert result.status == "infeasible"


def test_unstaffable_course_fails_loudly_but_still_schedules(db_manager):
    """A course no one can teach: named in the warnings, scheduled unstaffed."""
    with db_manager.get_session() as session:
        # F1 explicitly lists OTHER101 only; no unrestricted instructors.
        _seed(session, approved=("OTHER101",))
        session.add(Course(code="OTHER101", title="O", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"]))
        session.commit()
        engine = _engine(session, load_limit=False)
        sections = engine.create_sections("2026FA")
        result = engine.solve(sections, "Unstaffable Test", "2026FA")
        assert result.status in ("optimal", "feasible")
        assert any(
            "UNSTAFFABLE: AAA101" in w for w in result.staffing_warnings
        ), result.staffing_warnings
        aaa = [s for s in result.sections if s.course_code == "AAA101"]
        assert aaa and aaa[0].faculty_id is None


def test_ingest_overload_column(db_manager, tmp_path):
    (tmp_path / "faculty.csv").write_text(
        "id,name,email,max_credits,max_overload_credits,approved_courses\n"
        "F1,Prof One,a@t.co,12,6,\n"
        "F2,Prof Two,b@t.co,12,,\n"
        "F3,Prof Three,c@t.co,12,junk,\n"
        "F4,Prof Four,d@t.co,12,-3,\n"
    )
    with db_manager.get_session() as session:
        ing = DataIngestor(session, str(tmp_path))
        ing.load_faculty()
        assert session.get(Faculty, "F1").max_overload_credits == 6
        assert session.get(Faculty, "F2").max_overload_credits is None
        assert session.get(Faculty, "F3").max_overload_credits is None
        assert session.get(Faculty, "F4").max_overload_credits is None
        joined = " ".join(ing.errors)
        assert "F3" in joined and "F4" in joined


def test_doctor_flags_unstaffable_demanded_course(db_manager):
    from src.core.doctor import run_health_checks
    from src.database.models import ClassStanding, Student, StudentDemand

    with db_manager.get_session() as session:
        _seed(session, approved=("OTHER101",))
        session.add(Student(id="S1", name="S1", email="",
                            class_standing=ClassStanding.SR, credits_earned=95,
                            major="AAA", catalog_year=2024))
        session.add(StudentDemand(student_id="S1", course_code="AAA101",
                                  urgency_score=225.0, is_required=True,
                                  is_graduation_critical=True, reason="t"))
        session.commit()
        by = {r.name: r for r in run_health_checks(session, deep=False)}
        assert by["unstaffable-courses"].status == "fail"
        assert any("AAA101" in d for d in by["unstaffable-courses"].details)

        # An unrestricted instructor resolves it.
        session.add(Faculty(id="FADJ", name="Adj", email="", max_credits=12,
                            approved_courses=[]))
        session.commit()
        by = {r.name: r for r in run_health_checks(session, deep=False)}
        assert by["unstaffable-courses"].status == "ok"
