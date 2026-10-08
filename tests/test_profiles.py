"""Policy profiles: named weight bundles solved side-by-side."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.engine import SchedulerConfig
from src.core.profiles import (
    PROFILES,
    apply_profile,
    compare_profiles,
    render_profiles_markdown,
)
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
    ScheduleVersion,
    StructureType,
    Student,
    StudentDemand,
    TimeSlot,
)


@pytest.fixture
def source_db():
    """A solvable two-course dataset in a tmp DB file (path-based, since
    compare_profiles scratch-copies from a path)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    manager = DatabaseManager(db_path)
    manager.create_tables()
    with manager.get_session() as session:
        for i in (1, 2, 3):
            session.add(TimeSlot(id=i, name=f"MWF_{7 + i:02d}00", pattern="MWF",
                                 days=["M", "W", "F"],
                                 start_time=f"{7 + i:02d}:00",
                                 end_time=f"{7 + i:02d}:50",
                                 duration_minutes=50, slot_type="MWF_50"))
        for r in ("R1", "R2"):
            session.add(Room(id=r, building="B", number=r[-1], capacity=30,
                             room_type=RoomType.LECTURE))
        for code in ("CHEM101", "MATH101"):
            session.add(Course(code=code, title=code, credits=3,
                               structure_type=StructureType.STANDALONE,
                               required_room_types=["LECTURE"],
                               valid_slot_types=["MWF_50"], min_enrollment=2))
        session.add(Faculty(id="F1", name="Prof 1", email="1@t.co",
                            max_credits=12, approved_courses=["CHEM101"]))
        session.add(Faculty(id="F2", name="Prof 2", email="2@t.co",
                            max_credits=12, approved_courses=["MATH101"]))
        for i in range(6):
            sid = f"S{i + 1}"
            code = "CHEM101" if i < 3 else "MATH101"
            session.add(Student(id=sid, name=sid, email="",
                                class_standing=ClassStanding.SO,
                                credits_earned=30, major="CHEM",
                                catalog_year=2024))
            session.add(StudentDemand(student_id=sid, course_code=code,
                                      urgency_score=20.0, is_required=True,
                                      reason="t"))
        session.commit()
    yield db_path
    db_path.unlink(missing_ok=True)


def test_apply_profile_sets_overrides_without_global_mutation():
    cfg = apply_profile(SchedulerConfig(), "students_first")
    assert cfg.conflict_urgency_threshold == 20.0
    assert cfg.soft_constraints["faculty_prep_penalty"]["weight"] == -5
    # A fresh config still carries the shipped defaults.
    fresh = SchedulerConfig()
    assert fresh.conflict_urgency_threshold == 50.0
    assert fresh.soft_constraints["faculty_prep_penalty"]["weight"] == -15


def test_apply_profile_efficiency_enables_closure():
    cfg = apply_profile(SchedulerConfig(), "efficiency_first")
    assert cfg.section_settings["allow_section_closure"] is True
    assert cfg.soft_constraints["open_section_penalty"]["weight"] == -40
    assert SchedulerConfig().section_settings["allow_section_closure"] is False


def test_apply_profile_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown profile"):
        apply_profile(SchedulerConfig(), "chaos_first")


def test_all_declared_profiles_have_descriptions():
    for name, p in PROFILES.items():
        assert p.get("description"), name


def test_compare_profiles_end_to_end(source_db):
    result = compare_profiles(
        source_db, "2026FA", ["balanced", "faculty_first"], time_limit=25
    )
    assert [p["name"] for p in result["profiles"]] == ["balanced", "faculty_first"]
    for p in result["profiles"]:
        assert p["status"] in ("optimal", "feasible")
        m = p["metrics"]
        assert m["total_enrollment"] == 6, "tiny instance must seat everyone"
        assert m["mean_preps"] is not None and m["mean_preps"] >= 1
        assert m["mean_teaching_days"] is not None
        assert m["fill_pct"] and m["fill_pct"] > 0
        assert m["mean_days_on_campus"] is not None

    md = render_profiles_markdown(result)
    assert "| Metric | balanced | faculty_first |" in md
    assert "Trade-off table" in md
    assert "**Students** " in md or "| **Students** |" in md


def test_compare_profiles_never_writes_source(source_db):
    compare_profiles(source_db, "2026FA", ["balanced"], time_limit=20)
    manager = DatabaseManager(source_db)
    with manager.get_session() as session:
        schedules = list(session.exec(select(ScheduleVersion)).all())
    assert schedules == [], "solves must land on scratch copies only"


def test_compare_profiles_unknown_profile_raises(source_db):
    with pytest.raises(ValueError, match="Unknown profile"):
        compare_profiles(source_db, "2026FA", ["balanced", "nope"], time_limit=5)
