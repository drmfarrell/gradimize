"""What-if scenario runner: resource tweaks on a throwaway DB copy vs. baseline."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.engine import SchedulerConfig
from src.core.scenarios import (
    ScenarioTweaks,
    format_metric_delta,
    format_metric_value,
    run_scenario,
)
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
def source_db():
    """A seeded source database FILE (run_scenario copies it, twice)."""
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
    config.solver_settings["max_time_seconds"] = 30
    config.section_settings["students_per_section"] = students_per_section
    return config


def _add_demand(session, student_id: str, course_code: str, *, grad_critical: bool = True):
    """Student + demand row, inserted directly (run_scenario should NOT need
    to fall back to the internal DemandCalculator when demand already
    exists — see test's use of _fast_config without a demand_source)."""
    session.add(Student(id=student_id, name=student_id, email="",
                        class_standing=ClassStanding.FR, credits_earned=0,
                        major="CHEM", catalog_year=2024))
    session.add(StudentDemand(student_id=student_id, course_code=course_code,
                              urgency_score=50.0, is_required=True,
                              is_graduation_critical=grad_critical,
                              reason="Major requirement"))


def _seed_simple(session, *, students: int = 1, faculty_ids=("F1",), max_class_size=None):
    """One course, one room, one slot — trivially solvable by default."""
    session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                         start_time="09:00", end_time="09:50", duration_minutes=50,
                         slot_type="MWF_50"))
    session.add(Room(id="R1", building="B", number="1", capacity=30,
                     room_type=RoomType.LECTURE))
    session.add(Course(code="CHEM101", title="General Chemistry", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                       max_class_size=max_class_size))
    for fid in faculty_ids:
        session.add(Faculty(id=fid, name=f"Prof {fid}", email=f"{fid}@t.co",
                            max_credits=12, approved_courses=["CHEM101"]))
    for i in range(students):
        _add_demand(session, f"S{i + 1}", "CHEM101")
    session.commit()


def _seed_staffing_bottleneck(session):
    """Two courses; F1 is the ONLY instructor approved for CHEM101, and it
    also props up CHEM201's capacity alongside CHEM201's own dedicated F2.
    2 rooms x 3 slots leave plenty of placement room, so FACULTY coverage
    (not rooms) is the resource under test.
    """
    for i in (1, 2, 3):
        session.add(TimeSlot(id=i, name=f"MWF_{8 + i:02d}00", pattern="MWF",
                             days=["M", "W", "F"], start_time=f"{8 + i:02d}:00",
                             end_time=f"{8 + i:02d}:50", duration_minutes=50,
                             slot_type="MWF_50"))
    for rid in ("R1", "R2"):
        session.add(Room(id=rid, building="B", number=rid[-1], capacity=30,
                         room_type=RoomType.LECTURE))
    session.add(Course(code="CHEM101", title="General Chemistry", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
    session.add(Course(code="CHEM201", title="Organic Chemistry", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
    # F1: the only instructor approved for CHEM101 — and also approved for
    # CHEM201, where it quietly covers overflow beyond F2's own capacity.
    session.add(Faculty(id="F1", name="Prof One", email="f1@t.co", max_credits=12,
                        approved_courses=["CHEM101", "CHEM201"]))
    session.add(Faculty(id="F2", name="Prof Two", email="f2@t.co", max_credits=12,
                        approved_courses=["CHEM201"]))
    _add_demand(session, "S1", "CHEM101")
    for i in range(2, 6):  # S2..S5: 4 students want CHEM201
        _add_demand(session, f"S{i}", "CHEM201")
    session.commit()


def test_format_metric_helpers():
    assert format_metric_value("sections", 5) == "5"
    assert format_metric_value("room_util_pct", 12.345) == "12.3%"
    assert format_metric_value("sections", None) == "—"
    assert format_metric_delta("sections", 3) == "+3"
    assert format_metric_delta("sections", -2) == "-2"
    assert format_metric_delta("room_util_pct", 1.26) == "+1.3pp"
    assert format_metric_delta("sections", None) == "—"


def test_remove_sole_instructor_shows_up_as_worse(source_db):
    """Losing the shared instructor overloads the course it was propping up:
    F1 is CHEM101's only approver AND CHEM201's overflow capacity beyond
    F2. Removing F1 leaves CHEM201 needing 4 section-meetings from F2
    alone, who can cover at most 3 (only 3 slots exist) — a genuine
    staffing bottleneck that wasn't there in the baseline.
    """
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_staffing_bottleneck(session)

    tweaks = ScenarioTweaks(remove_faculty=["F1"])
    result = run_scenario(
        db_path, "2026FA", tweaks,
        config=_fast_config(students_per_section=1), time_limit=30,
    )

    assert result.baseline_status in ("optimal", "feasible")
    assert result.baseline_staffing_warnings == []
    assert result.baseline_metrics["total_enrollment"] == 5
    assert (
        result.scenario_metrics["total_enrollment"]
        < result.baseline_metrics["total_enrollment"]
        or len(result.scenario_staffing_warnings) > len(result.baseline_staffing_warnings)
    )


def test_add_room_and_faculty_round_trip(source_db):
    """Tweaked copy gets the new rows, baseline copy doesn't, source is untouched."""
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_simple(session)

    tweaks = ScenarioTweaks(
        add_faculty=[{"id": "FNEW", "name": "New Prof",
                     "approved_courses": ["CHEM101"], "max_credits": 9}],
        add_rooms=[{"id": "RNEW", "building": "New Building", "number": "100",
                   "capacity": 25, "room_type": "LECTURE"}],
    )
    result = run_scenario(db_path, "2026FA", tweaks, config=_fast_config(), time_limit=30)

    with DatabaseManager(result.scenario_db_path).get_session() as s:
        assert {f.id for f in s.exec(select(Faculty)).all()} == {"F1", "FNEW"}
        assert {r.id for r in s.exec(select(Room)).all()} == {"R1", "RNEW"}

    with DatabaseManager(result.baseline_db_path).get_session() as s:
        assert {f.id for f in s.exec(select(Faculty)).all()} == {"F1"}
        assert {r.id for r in s.exec(select(Room)).all()} == {"R1"}

    with manager.get_session() as s:
        assert {f.id for f in s.exec(select(Faculty)).all()} == {"F1"}
        assert {r.id for r in s.exec(select(Room)).all()} == {"R1"}


def test_set_max_class_size_applied(source_db):
    """The cap lands on the scenario copy's course row, not the baseline's."""
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_simple(session, max_class_size=None)

    tweaks = ScenarioTweaks(set_max_class_size={"CHEM101": 5})
    result = run_scenario(db_path, "2026FA", tweaks, config=_fast_config(), time_limit=30)

    with DatabaseManager(result.scenario_db_path).get_session() as s:
        assert s.get(Course, "CHEM101").max_class_size == 5

    with DatabaseManager(result.baseline_db_path).get_session() as s:
        assert s.get(Course, "CHEM101").max_class_size is None


def test_deltas_arithmetic(source_db):
    """Adding a room relieves a room-count pigeonhole: 0 enrolled -> 2 enrolled.

    2 students + students_per_section=1 need 2 sections, but the source has
    only 1 room x 1 slot = 1 (room, slot) pair, so baseline is infeasible
    (two mandatory placements can't share the one pair). Adding a 2nd room
    gives 2 pairs, which fixes it — a clean, deterministic non-zero delta.
    """
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_simple(session, students=2, faculty_ids=("F1", "F2"))

    tweaks = ScenarioTweaks(
        add_rooms=[{"id": "R2", "building": "B", "number": "2",
                   "capacity": 30, "room_type": "LECTURE"}]
    )
    result = run_scenario(
        db_path, "2026FA", tweaks,
        config=_fast_config(students_per_section=1), time_limit=30,
    )

    assert result.baseline_status == "infeasible"
    assert result.baseline_metrics["total_enrollment"] == 0
    assert result.scenario_status in ("optimal", "feasible")
    assert result.scenario_metrics["total_enrollment"] == 2

    for key, base_val in result.baseline_metrics.items():
        scen_val = result.scenario_metrics.get(key)
        if isinstance(base_val, (int, float)) and isinstance(scen_val, (int, float)):
            assert result.deltas[key] == scen_val - base_val
    assert result.deltas["total_enrollment"] == 2
    assert result.deltas["sections"] == 2


def test_source_database_untouched(source_db):
    """run_scenario never opens the SOURCE file for writing, even though the
    scenario copy both loses (remove_faculty) and gains (add_faculty) rows.
    """
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_simple(session)

    tweaks = ScenarioTweaks(
        add_faculty=[{"id": "FNEW", "approved_courses": ["CHEM101"]}],
        remove_faculty=["F1"],
    )
    run_scenario(db_path, "2026FA", tweaks, config=_fast_config(), time_limit=30)

    with manager.get_session() as session:
        assert [f.id for f in session.exec(select(Faculty)).all()] == ["F1"]
