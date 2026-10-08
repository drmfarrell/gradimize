"""Multi-semester projection: capacity-limited progression on a scratch copy."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.cadence import derive_standing, next_term
from src.core.engine import SchedulerConfig
from src.core.longrange import project_terms
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Faculty,
    FreshmenProjection,
    Prerequisite,
    Room,
    RoomType,
    StructureType,
    Student,
    StudentCourseHistory,
    TimeSlot,
)


@pytest.fixture
def source_db():
    """A seeded source database FILE (the projector copies it)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield db_path, manager
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def _seed(session, *, students=2, course_a_cap=None):
    """Two-course prereq chain (A -> B), both required for the CSCI major."""
    for i in (1, 2):
        session.add(TimeSlot(id=i, name=f"MWF_{8 + i:02d}00", pattern="MWF",
                             days=["M", "W", "F"], start_time=f"{8 + i:02d}:00",
                             end_time=f"{8 + i:02d}:50", duration_minutes=50,
                             slot_type="MWF_50"))
    session.add(Room(id="R1", building="B", number="1", capacity=30,
                     room_type=RoomType.LECTURE))
    session.add(Course(code="CSCI101", title="Intro", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                       max_class_size=course_a_cap))
    session.add(Course(code="CSCI201", title="Data Structures", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
    session.add(Prerequisite(course_code="CSCI201", prereq_code="CSCI101",
                             min_grade="D"))
    for code in ("CSCI101", "CSCI201"):
        session.add(DegreeRequirement(major="CSCI", course_code=code,
                                      is_required=True, category="core",
                                      catalog_year=2024))
    session.add(Faculty(id="F1", name="Prof", email="p@t.co", max_credits=12,
                        approved_courses=["CSCI101", "CSCI201"]))
    for i in range(students):
        session.add(Student(id=f"S{i + 1}", name=f"Student {i + 1}", email="",
                            class_standing=ClassStanding.FR, credits_earned=0,
                            major="CSCI", catalog_year=2024))
    session.commit()


def _fast_config():
    config = SchedulerConfig()
    config.solver_settings["max_time_seconds"] = 30
    return config


def test_next_term_and_standing():
    assert next_term("2026FA") == "2027SP"
    assert next_term("2027SP") == "2027FA"
    assert derive_standing(0) == ClassStanding.FR
    assert derive_standing(45) == ClassStanding.SO
    assert derive_standing(89) == ClassStanding.JR
    assert derive_standing(90) == ClassStanding.SR


def test_prereq_chain_progression(source_db):
    """Term 1 offers A (B is prereq-gated); term 2 offers B after passing A."""
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed(session)

    results, scratch = project_terms(
        db_path, "2026FA", 2, config=_fast_config(), time_limit_per_term=30
    )

    assert [r.term for r in results] == ["2026FA", "2027SP"]
    t1, t2 = results
    assert t1.status in ("optimal", "feasible")
    assert "CSCI101" in t1.by_course and t1.by_course["CSCI101"].enrolled == 2
    assert "CSCI201" not in t1.by_course, "B must be prereq-gated in term 1"
    assert t2.status in ("optimal", "feasible")
    assert "CSCI201" in t2.by_course and t2.by_course["CSCI201"].enrolled == 2

    # The scratch copy carries the simulated history for term 1.
    scratch_mgr = DatabaseManager(scratch)
    with scratch_mgr.get_session() as s:
        hist = s.exec(select(StudentCourseHistory)).all()
        assert {(h.course_code, h.semester) for h in hist} >= {
            ("CSCI101", "2026FA"), ("CSCI201", "2027SP")
        }


def test_capacity_limited_progression(source_db):
    """A 1-seat class: the unseated student re-demands it next term."""
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed(session, students=2, course_a_cap=1)

    results, _scratch = project_terms(
        db_path, "2026FA", 2, config=_fast_config(), time_limit_per_term=30
    )
    t1, t2 = results
    assert t1.by_course["CSCI101"].enrolled == 1
    assert t1.by_course["CSCI101"].unmet == 1
    # Next term: the seated student moved on to B; the other still needs A.
    assert t2.by_course.get("CSCI101") is not None
    assert t2.by_course["CSCI101"].demand == 1
    assert t2.by_course.get("CSCI201") is not None
    assert t2.by_course["CSCI201"].demand == 1


def test_graduation_and_stop(source_db):
    """A student finishing their last course graduates; empty demand stops."""
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed(session, students=1)
        s1 = session.get(Student, "S1")
        s1.credits_earned = 118
        s1.class_standing = ClassStanding.SR
        session.add(StudentCourseHistory(student_id="S1", course_code="CSCI101",
                                         grade="A", semester="2025FA",
                                         credits_earned=3))
        session.add(s1)
        session.commit()

    results, _scratch = project_terms(
        db_path, "2026FA", 3, config=_fast_config(), time_limit_per_term=30
    )
    t1 = results[0]
    assert t1.by_course.get("CSCI201") is not None, "only B remains"
    assert t1.enrollments == 1
    t2 = results[1]
    assert t2.graduates == 1
    assert t2.status == "empty"
    assert len(results) == 2, "projection stops when demand runs out"


def test_freshmen_inflow_each_fall(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed(session, students=1)
        session.add(FreshmenProjection(major="CSCI", estimated_count=3,
                                       catalog_year=2026, semester="",
                                       is_active=True))
        session.commit()

    results, _scratch = project_terms(
        db_path, "2026FA", 1, config=_fast_config(), time_limit_per_term=30
    )
    t1 = results[0]
    assert t1.freshmen_added == 3
    assert t1.students == 4
    assert t1.by_course["CSCI101"].demand == 4


def test_source_database_untouched(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed(session, students=2)

    project_terms(db_path, "2026FA", 2, config=_fast_config(),
                  time_limit_per_term=30)

    with manager.get_session() as session:
        assert len(session.exec(select(Student)).all()) == 2
        assert session.exec(select(StudentCourseHistory)).all() == []
