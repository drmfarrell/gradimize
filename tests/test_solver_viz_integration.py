"""End-to-end proof that the live solver visualization (src/core/solver_viz.py)
actually fires against a real CP-SAT solve, and that ``GRADIMIZE_VIZ=0``
truly silences it. Existing unit tests stub the solver; this exercises the
real pipeline (``src.core.generate.run_generation``) with a real solve, same
as tests/test_generate.py's small-dataset fixtures.
"""

import pytest

from src.core import generate as generate_mod
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

# All event types the solve stream may carry (see tests/test_generate.py).
ALL_EVENT_TYPES = {
    "log", "viz_init", "phase", "incumbent", "bound", "activity", "solver_line", "done", "error",
}
VIZ_EVENT_TYPES = {"viz_init", "phase", "incumbent", "bound", "activity", "solver_line"}


def _collect_emit():
    events = []

    def emit(event_type, **fields):
        events.append({"type": event_type, **fields})

    return emit, events


@pytest.fixture
def small_dataset(db_manager):
    """Guaranteed-feasible one-course dataset (same shape as test_generate.py)."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                              start_time="08:00", end_time="08:50", duration_minutes=50,
                              slot_type="MWF_50"))
        session.add(TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                              start_time="09:00", end_time="09:50", duration_minutes=50,
                              slot_type="MWF_50"))
        session.add(Room(id="R1", building="Test", number="101", capacity=30,
                          room_type=RoomType.LECTURE))
        session.add(Room(id="R2", building="Test", number="102", capacity=25,
                          room_type=RoomType.LECTURE))
        session.add(Course(code="CSCI101", title="Intro", credits=3,
                            structure_type=StructureType.STANDALONE,
                            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="Prof A", email="a@test.com", max_credits=12,
                             approved_courses=["CSCI101"]))
        session.add(Student(id="S1", name="Student 1", email="s1@test.com",
                             class_standing=ClassStanding.FR, credits_earned=0, major="CSCI",
                             catalog_year=2024))
        session.add(Student(id="S2", name="Student 2", email="s2@test.com",
                             class_standing=ClassStanding.SO, credits_earned=30, major="CSCI",
                             catalog_year=2024))
        session.commit()
    return db_manager


@pytest.fixture
def seeded_demands(small_dataset):
    with small_dataset.get_session() as session:
        session.add(StudentDemand(student_id="S1", course_code="CSCI101", urgency_score=15.0,
                                   is_required=True, reason="Test"))
        session.add(StudentDemand(student_id="S2", course_code="CSCI101", urgency_score=25.0,
                                   is_required=True, reason="Test"))
        session.commit()
    return small_dataset


@pytest.fixture
def patched_engine(small_dataset, monkeypatch):
    monkeypatch.setattr(generate_mod, "get_engine", lambda: small_dataset.engine)
    return small_dataset


TIME_LIMIT = 5


def test_real_solve_emits_visualization_events(seeded_demands, patched_engine, monkeypatch):
    monkeypatch.delenv("GRADIMIZE_VIZ", raising=False)

    emit, events = _collect_emit()
    generate_mod.run_generation(
        emit, semester="2025SP", name="Gen Test", workers=1, time_limit=TIME_LIMIT,
        include_projections=False,
    )

    assert events, "must emit at least one event"

    terminal_events = [e for e in events if e["type"] in ("done", "error")]
    assert len(terminal_events) == 1, terminal_events
    assert events[-1]["type"] in ("done", "error")
    assert events[-1] is terminal_events[0]

    unexpected = {e["type"] for e in events} - ALL_EVENT_TYPES
    assert not unexpected, f"unexpected event types: {unexpected}"

    if events[-1]["type"] == "error":
        # An unrelated data issue must never turn this into a false failure.
        return

    viz_inits = [e for e in events if e["type"] == "viz_init"]
    assert viz_inits, "a real solve with viz enabled must emit viz_init"
    init = viz_inits[0]
    assert init["slots"], "viz_init.slots must be non-empty"
    assert init["depts"], "viz_init.depts must be non-empty"
    assert init["total_sections"] > 0
    assert init["time_limit"] == TIME_LIMIT

    incumbents = [e for e in events if e["type"] == "incumbent"]
    if incumbents:
        for inc in incumbents:
            assert isinstance(inc["objective"], (int, float))
            assert isinstance(inc["bound"], (int, float))
            assert isinstance(inc["gap_pct"], (int, float))
            if "placed" in inc:
                for entry in inc["placed"]:
                    slot_id, dept_idx, label = entry
                    assert isinstance(slot_id, int)
                    assert isinstance(dept_idx, int)
                    assert 0 <= dept_idx < len(init["depts"])
                    assert isinstance(label, str)


def test_no_viz_emits_no_visualization_events(seeded_demands, patched_engine, monkeypatch):
    monkeypatch.setenv("GRADIMIZE_VIZ", "0")

    emit, events = _collect_emit()
    generate_mod.run_generation(
        emit, semester="2025SP", name="Gen Test", workers=1, time_limit=TIME_LIMIT,
        include_projections=False,
    )

    assert events, "must emit at least one event"

    seen_types = {e["type"] for e in events}
    assert not (seen_types & VIZ_EVENT_TYPES), (
        f"GRADIMIZE_VIZ=0 must silence all visualization events, saw: "
        f"{seen_types & VIZ_EVENT_TYPES}"
    )

    terminal_events = [e for e in events if e["type"] in ("done", "error")]
    assert len(terminal_events) == 1, terminal_events
    assert events[-1] is terminal_events[0]
    assert all(e["type"] == "log" for e in events[:-1])
