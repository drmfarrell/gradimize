"""Tests for the SSE streaming endpoints in src/api/routes/schedule.py:
GET /schedule/generate/stream and GET /schedule/{id}/re-solve/stream.

Also covers the job-lock 409 contract for these two endpoints (the other
half of that contract — /data/import/{kind} — is in test_api_job_lock.py).
Zero coverage before this file for both concerns.

Datasets are a single course/section so the real CP-SAT solve driven through
the stream stays well under a second, keeping the HTTP request (which blocks
until the stream completes) fast and deterministic.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from src.api.routes.schedule import get_session, router
from src.core import generate as generate_mod
from src.core import job_lock
from src.core.conflicts import build_conflict_matrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
    ScheduleVersion,
    Section,
    StructureType,
    Student,
    StudentDemand,
    TimeSlot,
)


@pytest.fixture(autouse=True)
def _job_lock_safety_net():
    """job_lock is a process-wide singleton (src/core/job_lock.py). Make
    sure a failed assertion here can never leak a held lock into another
    test, in this file or any other."""
    job_lock.release()
    yield
    job_lock.release()


@pytest.fixture
def seeded(db_manager):
    """One course, one section's worth of demand — small enough that the
    real solver returns almost instantly."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                              start_time="08:00", end_time="08:50", duration_minutes=50,
                              slot_type="MWF_50"))
        session.add(Room(id="R1", building="Test", number="101", capacity=30,
                          room_type=RoomType.LECTURE))
        session.add(Course(code="CSCI101", title="Intro", credits=3,
                            structure_type=StructureType.STANDALONE,
                            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="Prof A", email="a@test.com", max_credits=12,
                             approved_courses=["CSCI101"]))
        session.add(Student(id="S1", name="Student 1", email="s1@test.com",
                             class_standing=ClassStanding.FR, credits_earned=0, major="CSCI",
                             catalog_year=2024))
        session.add(StudentDemand(student_id="S1", course_code="CSCI101", urgency_score=15.0,
                                   is_required=True, reason="Test"))
        session.commit()
    return db_manager


@pytest.fixture
def patched_engine(seeded, monkeypatch):
    """The stream endpoints call src.core.generate.run_generation/run_resolve,
    which open Session(get_engine()) with no path — point that at the
    hermetic temp DB instead of the process-wide default."""
    monkeypatch.setattr(generate_mod, "get_engine", lambda: seeded.engine)
    return seeded


@pytest.fixture
def client(patched_engine):
    app = FastAPI()
    app.include_router(router)

    def override_session():
        with patched_engine.get_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


@pytest.fixture
def existing_schedule_id(patched_engine):
    """A already-solved schedule to re-solve, built directly through the
    engine (not through the API) to keep this fixture fast and independent
    of the generate-stream endpoint under test elsewhere."""
    with patched_engine.get_session() as session:
        conflict_matrix = build_conflict_matrix(session)
        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 5
        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Original", "2025SP")
        assert result.status in ("optimal", "feasible"), result.status
        return result.sections[0].schedule_id


# Non-terminal event types the solve stream may carry. "log" is the original
# progress text; the rest are live-visualization events (src/core/solver_viz.py).
STREAM_EVENT_TYPES = {"log", "viz_init", "phase", "incumbent", "bound", "activity", "solver_line"}


def _parse_sse_events(text: str) -> list[dict]:
    """Extract the JSON payload of every `data: ...` SSE frame, in order.
    Heartbeat `: ping` comment lines are not prefixed "data: " and are
    dropped by this filter, same as a real EventSource client would."""
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


# ─────────────────────────────────────────────────────────────────
# GET /schedule/generate/stream
# ─────────────────────────────────────────────────────────────────


def test_generate_stream_frames_are_logs_then_one_done_event(client, patched_engine):
    resp = client.get(
        "/schedule/generate/stream",
        params={"semester": "2025SP", "name": "Stream Test", "workers": 1, "time_limit": 5},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse_events(resp.text)
    assert events, "expected at least one SSE data frame"
    assert events[-1]["type"] == "done", events[-1]
    unexpected = {e["type"] for e in events[:-1]} - STREAM_EVENT_TYPES
    assert not unexpected, f"unexpected event types: {unexpected}"

    schedule_id = events[-1]["schedule_id"]
    assert schedule_id is not None
    assert events[-1]["status"] in ("optimal", "feasible")

    with patched_engine.get_session() as session:
        assert session.get(ScheduleVersion, schedule_id) is not None
        sections = session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
        assert len(sections) > 0


def test_generate_stream_409_when_job_lock_held(client):
    assert job_lock.try_acquire("generate", "someone else") is True
    try:
        resp = client.get("/schedule/generate/stream", params={"time_limit": 5})
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]
    finally:
        job_lock.release()


def test_generate_stream_releases_lock_after_completion(client):
    """The lock must be free again after a run finishes, so the next request
    isn't permanently blocked."""
    resp = client.get(
        "/schedule/generate/stream",
        params={"semester": "2025SP", "workers": 1, "time_limit": 5},
    )
    assert resp.status_code == 200
    assert job_lock.status()["running"] is False


# ─────────────────────────────────────────────────────────────────
# GET /schedule/{id}/re-solve/stream
# ─────────────────────────────────────────────────────────────────


def test_resolve_stream_frames_are_logs_then_one_done_event(
    client, patched_engine, existing_schedule_id
):
    resp = client.get(
        f"/schedule/{existing_schedule_id}/re-solve/stream",
        params={"workers": 1, "time_limit": 5},
    )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    assert events[-1]["type"] == "done", events[-1]
    unexpected = {e["type"] for e in events[:-1]} - STREAM_EVENT_TYPES
    assert not unexpected, f"unexpected event types: {unexpected}"

    new_id = events[-1]["schedule_id"]
    assert new_id != existing_schedule_id

    with patched_engine.get_session() as session:
        assert session.get(ScheduleVersion, new_id) is not None


def test_resolve_stream_409_when_job_lock_held(client, existing_schedule_id):
    assert job_lock.try_acquire("re-solve", "someone else") is True
    try:
        resp = client.get(
            f"/schedule/{existing_schedule_id}/re-solve/stream",
            params={"time_limit": 5},
        )
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]
        # A rejected try_acquire() must not clear the lock someone else holds.
        assert job_lock.status()["running"] is True
    finally:
        job_lock.release()
