"""Enrollment stress test: scaled freshmen projections on scratch DB copies."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.engine import SchedulerConfig
from src.core.stress import render_stress_markdown, stress_test
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Faculty,
    FreshmenProjection,
    Room,
    RoomType,
    StructureType,
    Student,
    StudentDemand,
    TimeSlot,
)


@pytest.fixture
def source_db():
    """A seeded source database FILE (stress_test copies it, once per factor)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield db_path, manager
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def _fast_config(students_per_section: int = 25) -> SchedulerConfig:
    config = SchedulerConfig()
    config.solver_settings["max_time_seconds"] = 20
    config.section_settings["students_per_section"] = students_per_section
    return config


def _add_real_demand(session, student_id: str, course_code: str) -> None:
    """A real (non-projected) student + demand row, inserted directly —
    held constant across every factor (see test_scenarios.py's _add_demand,
    the same pattern: only the FreshmenProjection-derived synthetic cohort
    is supposed to move between variants)."""
    session.add(Student(id=student_id, name=student_id, email="",
                        class_standing=ClassStanding.SO, credits_earned=30,
                        major="CHEM", catalog_year=2024))
    session.add(StudentDemand(student_id=student_id, course_code=course_code,
                              urgency_score=20.0, is_required=True,
                              is_graduation_critical=False,
                              reason="Major requirement"))


def _seed_common(
    session, *, room_capacity: int, min_enrollment: int = 1,
    real_students: int = 2, projection_count: int = 6,
) -> None:
    """One course (CHEM101), one room/slot/faculty; `real_students` real
    students hold constant demand, plus a FreshmenProjection row of
    `projection_count` for major PHYS with a matching DegreeRequirement so
    ProjectionDemandCalculator's representative-student demand calc lands
    on CHEM101 (same recipe as test_longrange.py's test_freshmen_inflow_each_fall).
    """
    session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                         start_time="09:00", end_time="09:50", duration_minutes=50,
                         slot_type="MWF_50"))
    session.add(Room(id="R1", building="B", number="1", capacity=room_capacity,
                     room_type=RoomType.LECTURE))
    session.add(Course(code="CHEM101", title="General Chemistry", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                       min_enrollment=min_enrollment))
    session.add(Faculty(id="F1", name="Prof One", email="f1@t.co", max_credits=12,
                        approved_courses=["CHEM101"]))
    session.add(DegreeRequirement(major="PHYS", course_code="CHEM101",
                                  is_required=True, category="core", catalog_year=2024))
    for i in range(real_students):
        _add_real_demand(session, f"S{i + 1}", "CHEM101")
    session.add(FreshmenProjection(major="PHYS", estimated_count=projection_count,
                                   catalog_year=2024, semester="", is_active=True))
    session.commit()


def test_three_variants_monotone_enrollment(source_db):
    """Generous room (50 seats, nothing capacity-bound): enrolled count
    tracks total demand (2 real + scaled projected) and strictly increases
    with the factor.

    Deterministic scaling (max(0, round(6 * factor))):
      factor=0.8 -> round(4.8)=5  -> demand 2+5=7
      factor=1.0 -> round(6.0)=6  -> demand 2+6=8
      factor=1.2 -> round(7.2)=7  -> demand 2+7=9
    """
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_common(session, room_capacity=50)

    result = stress_test(
        db_path, "2026FA", factors=[0.8, 1.0, 1.2],
        time_limit=20, config=_fast_config(),
    )

    assert result["baseline_factor"] == 1.0
    assert len(result["variants"]) == 3
    for v in result["variants"]:
        assert v["status"] in ("optimal", "feasible")

    enrolled = {v["factor"]: v["metrics"]["total_enrollment"] for v in result["variants"]}
    assert enrolled == {0.8: 7, 1.0: 8, 1.2: 9}
    assert enrolled[0.8] < enrolled[1.0] < enrolled[1.2]

    # Fragility keys are always present in the return shape, even when (as
    # here, with a 50-seat room) nothing gets flagged.
    assert result["fragile_low"] == []
    assert result["fragile_high"] == []


def test_fragile_high_overflow_detected(source_db):
    """Room seats exactly 8. Hand-computed demand per factor (real=2 held
    constant; PHYS freshmen projection=6 scaled deterministically):

      factor=0.8 -> round(6*0.8)=5 -> demand 2+5=7  (<= 8: fits, unmet=0)
      factor=1.0 -> round(6*1.0)=6 -> demand 2+6=8  (== 8: exactly fits,
                                                       baseline unmet=0)
      factor=1.2 -> round(6*1.2)=7 -> demand 2+7=9  (>  8: 1 student
                                                       unseated, unmet=1)

    So CHEM101 must show unmet demand ONLY at factor 1.2 and must appear in
    fragile_high, triggered at exactly {1.2} — the literal
    "unmet demand appears at factor >= 1.1 but not at 1.0" rule from the
    spec, not the raw-metrics fallback.
    """
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_common(session, room_capacity=8, min_enrollment=1)

    result = stress_test(
        db_path, "2026FA", factors=[0.8, 1.0, 1.2],
        time_limit=20, config=_fast_config(),
    )

    by_factor = {v["factor"]: v for v in result["variants"]}
    for v in result["variants"]:
        assert v["status"] in ("optimal", "feasible")
    assert by_factor[0.8]["metrics"]["by_course"]["CHEM101"]["unmet"] == 0
    assert by_factor[1.0]["metrics"]["by_course"]["CHEM101"]["unmet"] == 0
    assert by_factor[1.2]["metrics"]["by_course"]["CHEM101"]["unmet"] == 1
    assert by_factor[1.2]["metrics"]["by_course"]["CHEM101"]["enrolled"] == 8
    assert by_factor[1.2]["metrics"]["by_course"]["CHEM101"]["demand"] == 9

    assert len(result["fragile_high"]) == 1
    flagged = result["fragile_high"][0]
    assert flagged["course_code"] == "CHEM101"
    assert {t["factor"] for t in flagged["triggers"]} == {1.2}
    assert result["fragile_low"] == []


def test_no_active_projections_short_circuits(source_db):
    """No active FreshmenProjection rows: return "nothing to stress"
    immediately rather than solving identical variants."""
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_common(session, room_capacity=50)
        proj = session.exec(select(FreshmenProjection)).one()
        proj.is_active = False
        session.add(proj)
        session.commit()

    result = stress_test(
        db_path, "2026FA", factors=[0.8, 1.0, 1.2],
        time_limit=20, config=_fast_config(),
    )

    assert result["nothing_to_stress"] is True
    assert result["variants"] == []
    assert result["fragile_low"] == []
    assert result["fragile_high"] == []
    assert "no active" in result["message"].lower()


def test_source_database_untouched(source_db):
    """FreshmenProjection counts and Student rows in the SOURCE database are
    unchanged after stress_test scales/regenerates demand only inside
    per-factor scratch copies (see test_scenarios.py /
    test_longrange.py's identically-named tests for the same invariant)."""
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_common(session, room_capacity=50)

    stress_test(
        db_path, "2026FA", factors=[0.8, 1.0, 1.2],
        time_limit=20, config=_fast_config(),
    )

    with manager.get_session() as session:
        projections = session.exec(select(FreshmenProjection)).all()
        assert len(projections) == 1
        assert projections[0].estimated_count == 6  # unscaled

        students = session.exec(select(Student)).all()
        assert len(students) == 2  # only the 2 real students seeded
        assert {s.id for s in students} == {"S1", "S2"}
        assert all(not s.is_projected for s in students)


def test_markdown_render_has_table_and_summary(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_common(session, room_capacity=50)

    result = stress_test(
        db_path, "2026FA", factors=[0.8, 1.0, 1.2],
        time_limit=20, config=_fast_config(),
    )
    md = render_stress_markdown(result)

    assert "## Executive summary" in md
    assert "| Metric |" in md
    assert "1.0x" in md  # baseline column header present
    assert "PROJECTED" in md  # standing caveat: returning-student demand held constant


def test_markdown_render_nothing_to_stress():
    """Pure render-side check of the short-circuit branch (no DB/solver
    needed): the "nothing to stress" message must show up in the markdown."""
    result = {
        "semester": "2026FA",
        "factors": [0.8, 1.0, 1.2],
        "variants": [],
        "fragile_low": [],
        "fragile_high": [],
        "baseline_factor": 1.0,
        "nothing_to_stress": True,
        "message": "No active FreshmenProjection rows found — nothing to stress.",
    }
    md = render_stress_markdown(result)
    assert "nothing to stress" in md.lower()
