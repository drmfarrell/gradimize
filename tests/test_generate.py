"""Tests for src/core/generate.py: the emit-driven generation/re-solve
pipeline shared by the TUI and the web SSE endpoints, plus the
demand-source-aware bootstrap_demands() helper. Zero coverage before this
file.

Datasets are kept to a single course/section so the real CP-SAT solves in
here stay well under a second.
"""

from datetime import datetime

import pytest
from sqlmodel import select

from src.core import generate as generate_mod
from src.core.conflicts import build_conflict_matrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Faculty,
    FreshmenProjection,
    Room,
    RoomType,
    ScheduleVersion,
    Section,
    StructureType,
    Student,
    StudentDemand,
    TimeSlot,
)

# Non-terminal event types the solve stream may carry. "log" is the original
# progress text; the rest are live-visualization events (src/core/solver_viz.py).
STREAM_EVENT_TYPES = {"log", "viz_init", "phase", "incumbent", "bound", "activity", "solver_line"}


def _collect_emit():
    events = []

    def emit(event_type, **fields):
        events.append({"type": event_type, **fields})

    return emit, events


@pytest.fixture
def small_dataset(db_manager):
    """Guaranteed-feasible one-course dataset (same shape as test_solver.py)."""
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
    """Pre-seed real (non-projected) demand so run_generation skips
    bootstrap_demands — isolates the emit-shape/order tests from the demand
    source question, which bootstrap_demands tests cover separately."""
    with small_dataset.get_session() as session:
        session.add(StudentDemand(student_id="S1", course_code="CSCI101", urgency_score=15.0,
                                   is_required=True, reason="Test"))
        session.add(StudentDemand(student_id="S2", course_code="CSCI101", urgency_score=25.0,
                                   is_required=True, reason="Test"))
        session.commit()
    return small_dataset


@pytest.fixture
def patched_engine(small_dataset, monkeypatch):
    """run_generation/run_resolve call get_engine() with no args, which
    normally resolves to the process-wide default DB path. Point that at the
    hermetic temp DB instead so the tests don't touch real data."""
    monkeypatch.setattr(generate_mod, "get_engine", lambda: small_dataset.engine)
    return small_dataset


# ─────────────────────────────────────────────────────────────────
# Pure helpers: _clean (markup stripping) and _failure_message
# ─────────────────────────────────────────────────────────────────


def test_clean_strips_lowercase_rich_markup():
    assert generate_mod._clean("[dim]hello[/dim]") == "hello"
    assert generate_mod._clean("[green bold]ok[/green bold]") == "ok"


def test_clean_preserves_uppercase_warn_style_text():
    assert generate_mod._clean("[WARN] something bad") == "[WARN] something bad"


def test_clean_rstrips_but_keeps_leading_whitespace():
    assert generate_mod._clean("  indented line  \n") == "  indented line"


class _FakeResult:
    def __init__(self, status, infeasible_core=None):
        self.status = status
        self.infeasible_core = infeasible_core


def test_failure_message_unknown_status_suggests_more_time():
    msg = generate_mod._failure_message(_FakeResult("unknown"))
    assert "unknown" in msg
    assert "increasing it" in msg


def test_failure_message_infeasible_without_core_is_bare():
    msg = generate_mod._failure_message(_FakeResult("infeasible", infeasible_core=None))
    assert msg == "Solver returned status: infeasible"


def test_failure_message_infeasible_with_core_names_families_and_hints():
    msg = generate_mod._failure_message(
        _FakeResult("infeasible", infeasible_core=["room-no-overlap", "capacity"])
    )
    assert "Conflicting requirements: room-no-overlap, capacity." in msg
    assert "Add rooms" in msg  # hint text for room-no-overlap
    assert "Enrollment cannot fit" in msg  # hint text for capacity


# ─────────────────────────────────────────────────────────────────
# bootstrap_demands: demand-source-aware fallback (both sources)
# ─────────────────────────────────────────────────────────────────


def test_bootstrap_demands_internal_source(small_dataset):
    with small_dataset.get_session() as session:
        session.add(DegreeRequirement(major="CSCI", catalog_year=2024, course_code="CSCI101",
                                       is_required=True, category="core"))
        session.commit()

        config = SchedulerConfig()
        config.demand_source = "internal"
        logs = []
        generate_mod.bootstrap_demands(session, config, logs.append)

        assert any("Calculating demands (internal)" in m for m in logs)
        assert any(m.startswith("Calculated ") for m in logs)
        demands = session.exec(select(StudentDemand)).all()
        assert len(demands) == 2  # S1 + S2 both need CSCI101
        assert all(d.is_projected is False for d in demands)


def test_bootstrap_demands_degreeworks_source_with_existing_csv(small_dataset, tmp_path):
    with small_dataset.get_session() as session:
        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE,BLOCK_TYPE\nS1,CSCI101,MAJOR\n")

        config = SchedulerConfig()
        config.demand_source = "degreeworks"
        config.degreeworks_csv = str(csv_path)
        logs = []
        generate_mod.bootstrap_demands(session, config, logs.append)

        assert any(m.startswith("Importing DegreeWorks audit:") for m in logs)
        assert any(m.startswith("DegreeWorks:") and "demands for" in m for m in logs)
        demands = session.exec(select(StudentDemand)).all()
        assert len(demands) == 1
        assert demands[0].reason.startswith("[DegreeWorks]")


def test_bootstrap_demands_degreeworks_source_missing_file_falls_back(small_dataset, tmp_path):
    with small_dataset.get_session() as session:
        session.add(DegreeRequirement(major="CSCI", catalog_year=2024, course_code="CSCI101",
                                       is_required=True, category="core"))
        session.commit()

        config = SchedulerConfig()
        config.demand_source = "degreeworks"
        config.degreeworks_csv = str(tmp_path / "does_not_exist.csv")
        logs = []
        generate_mod.bootstrap_demands(session, config, logs.append)

        assert any("not found" in m and "internal calculator" in m for m in logs)
        assert any("Calculating demands (internal)" in m for m in logs)
        demands = session.exec(select(StudentDemand)).all()
        assert len(demands) == 2  # fell through to the internal calculator


# ─────────────────────────────────────────────────────────────────
# run_generation: emit shape/order for a real small solve, plus branches
# ─────────────────────────────────────────────────────────────────


def test_run_generation_emits_logs_then_exactly_one_done_event(seeded_demands, patched_engine):
    emit, events = _collect_emit()

    generate_mod.run_generation(
        emit, semester="2025SP", name="Gen Test", workers=1, time_limit=5,
        include_projections=False,
    )

    assert events, "must emit at least one event"
    assert events[-1]["type"] == "done", events[-1]
    unexpected = {e["type"] for e in events[:-1]} - STREAM_EVENT_TYPES
    assert not unexpected, f"unexpected event types: {unexpected}"

    done = events[-1]
    assert done["status"] in ("optimal", "feasible")
    assert done["schedule_id"] is not None
    assert done["sections_count"] > 0
    assert done["enrollments_count"] >= 0
    assert isinstance(done["objective_value"], float)
    assert isinstance(done["solve_time_seconds"], float)

    msgs = [e["msg"] for e in events[:-1] if e["type"] == "log"]
    assert msgs[0] == "Using 2 existing student demands."
    assert "Building conflict matrix…" in msgs
    assert "Solver: 1 workers, 5s limit." in msgs
    assert any(m.startswith("Created ") and "sections to schedule" in m for m in msgs)
    assert any("Starting solver with" in m for m in msgs), "solver log_callback lines relayed"
    assert not any("[dim]" in m or "[/dim]" in m for m in msgs), "rich markup must be stripped"

    with patched_engine.get_session() as session:
        sv = session.get(ScheduleVersion, done["schedule_id"])
        assert sv is not None
        sections = session.exec(select(Section).where(Section.schedule_id == sv.id)).all()
        assert len(sections) == done["sections_count"]


def test_run_generation_include_projections_true_adds_synthetic_demand(
    small_dataset, patched_engine
):
    with small_dataset.get_session() as session:
        session.add(StudentDemand(student_id="S1", course_code="CSCI101", urgency_score=15.0,
                                   is_required=True, reason="Test"))
        session.add(DegreeRequirement(major="CSCI", catalog_year=2024, course_code="CSCI101",
                                       is_required=True, category="core"))
        session.add(FreshmenProjection(major="CSCI", estimated_count=2, catalog_year=2024,
                                        semester="2025SP", is_active=True))
        session.commit()

    emit, events = _collect_emit()
    generate_mod.run_generation(emit, semester="2025SP", name="Gen", workers=1, time_limit=5,
                                 include_projections=True)

    assert events[-1]["type"] == "done"
    msgs = [e["msg"] for e in events if e["type"] == "log"]
    assert any(m.startswith("Added 2 projected freshmen (2 demand records)") for m in msgs)


def test_run_generation_exclude_projections_clears_stale_projected_data(
    small_dataset, patched_engine
):
    with small_dataset.get_session() as session:
        session.add(StudentDemand(student_id="S1", course_code="CSCI101", urgency_score=15.0,
                                   is_required=True, reason="Test"))
        session.add(Student(id="PROJ_OLD_0001", name="Old Projected", email="x@test.com",
                             class_standing=ClassStanding.FR, credits_earned=0, major="CSCI",
                             catalog_year=2024, is_projected=True))
        session.add(StudentDemand(student_id="PROJ_OLD_0001", course_code="CSCI101",
                                   urgency_score=10.0, is_required=True, is_projected=True,
                                   reason="stale projection"))
        session.commit()

    emit, events = _collect_emit()
    generate_mod.run_generation(emit, semester="2025SP", name="Gen", workers=1, time_limit=5,
                                 include_projections=False)

    assert events[-1]["type"] == "done"
    msgs = [e["msg"] for e in events if e["type"] == "log"]
    assert any("cleared 1 stale projected students" in m for m in msgs)

    with patched_engine.get_session() as session:
        assert session.get(Student, "PROJ_OLD_0001") is None


def test_run_generation_exception_path_emits_single_error_event(
    seeded_demands, patched_engine, monkeypatch
):
    """An unexpected exception anywhere in the pipeline must be caught and
    surfaced as exactly one terminal "error" event, never propagated."""

    def _boom(session):
        raise RuntimeError("boom")

    monkeypatch.setattr(generate_mod, "build_conflict_matrix", _boom)

    emit, events = _collect_emit()
    generate_mod.run_generation(emit, semester="2025SP", name="Gen", workers=1, time_limit=5)

    assert events[-1] == {"type": "error", "message": "RuntimeError: boom"}
    assert all(e["type"] == "log" for e in events[:-1])


# ─────────────────────────────────────────────────────────────────
# run_resolve: emit shape/order + its own (non-solver) error paths
# ─────────────────────────────────────────────────────────────────


def test_run_resolve_schedule_not_found_emits_single_error(patched_engine):
    emit, events = _collect_emit()
    generate_mod.run_resolve(emit, schedule_id=9999, workers=1, time_limit=5)

    assert events == [{"type": "error", "message": "Schedule 9999 not found"}]


def test_run_resolve_schedule_with_no_sections_emits_single_error(patched_engine):
    with patched_engine.get_session() as session:
        session.add(ScheduleVersion(id=1, name="Empty", semester="2025SP", status="draft",
                                     created_at=datetime.fromisoformat("2026-01-01T00:00:00")))
        session.commit()

    emit, events = _collect_emit()
    generate_mod.run_resolve(emit, schedule_id=1, workers=1, time_limit=5)

    assert events == [{"type": "error", "message": "Schedule has no sections to re-solve"}]


def test_run_resolve_emits_logs_then_done_with_a_new_schedule(seeded_demands, patched_engine):
    # Build the original schedule directly through the engine (bypassing
    # run_generation) so this test stays focused on run_resolve's contract.
    with patched_engine.get_session() as session:
        conflict_matrix = build_conflict_matrix(session)
        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 5
        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Original", "2025SP")
        assert result.status in ("optimal", "feasible"), result.status
        original_id = result.sections[0].schedule_id

    emit, events = _collect_emit()
    generate_mod.run_resolve(
        emit, schedule_id=original_id, name="Resolved", workers=1, time_limit=5
    )

    assert events[-1]["type"] == "done", events[-1]
    unexpected = {e["type"] for e in events[:-1]} - STREAM_EVENT_TYPES
    assert not unexpected, f"unexpected event types: {unexpected}"

    done = events[-1]
    assert done["schedule_id"] != original_id
    assert done["status"] in ("optimal", "feasible")
    assert done["sections_count"] > 0

    msgs = [e["msg"] for e in events[:-1] if e["type"] == "log"]
    assert any(m.startswith("Re-solving 'Original':") and "0 pinned" in m for m in msgs)
    assert "Building conflict matrix…" in msgs
    assert "Solver: 1 workers, 5s limit." in msgs

    with patched_engine.get_session() as session:
        assert session.get(ScheduleVersion, done["schedule_id"]) is not None
