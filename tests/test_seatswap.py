"""Pacing-aware seat rebalancing: stage 2 of student scheduling."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.seatswap import rebalance_seats
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    Student,
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


def _slot(session, i, start, end):
    session.add(TimeSlot(id=i, name=f"MWF_{start.replace(':', '')}",
                         pattern="MWF", days=["M", "W", "F"], start_time=start,
                         end_time=end, duration_minutes=50, slot_type="MWF_50"))


def _section(session, schedule_id, crn, code, number, slot_id, capacity=10):
    s = Section(schedule_id=schedule_id, crn=crn, course_code=code,
                section_number=number, capacity=capacity,
                is_honors=False, is_pinned=False)
    session.add(s)
    session.commit()
    session.refresh(s)
    session.add(SectionMeeting(section_id=s.id, meeting_sequence=0,
                               time_slot_id=slot_id, room_id="R1"))
    session.commit()
    return s


def _enroll(session, section, *student_ids):
    for sid in student_ids:
        session.add(SectionEnrollment(section_id=section.id, student_id=sid,
                                      is_confirmed=False))
    session.commit()


def _student(session, sid, pacing=None):
    session.add(Student(id=sid, name=sid, email="",
                        class_standing=ClassStanding.SO, credits_earned=30,
                        major="CSCI", catalog_year=2024,
                        pacing_preference=pacing))
    session.commit()


@pytest.fixture
def gap_case(db_manager):
    """S1 has MATH at 9:00 and CSCI section A at 15:00 (a 5-hour gap);
    sibling CSCI section B meets at 10:00 with free seats."""
    with db_manager.get_session() as session:
        _slot(session, 1, "09:00", "09:50")
        _slot(session, 2, "10:00", "10:50")
        _slot(session, 3, "15:00", "15:50")
        sched = ScheduleVersion(name="T", created_at=datetime.fromisoformat("2026-01-01T00:00:00"),
                                semester="2026FA", status="draft",
                                solver_stats={})
        session.add(sched)
        session.commit()
        session.refresh(sched)

        math = _section(session, sched.id, "20001", "MATH101", "001", 1)
        csci_a = _section(session, sched.id, "20002", "CSCI101", "001", 3)
        csci_b = _section(session, sched.id, "20003", "CSCI101", "002", 2)
        _student(session, "S1")
        _enroll(session, math, "S1")
        _enroll(session, csci_a, "S1")
        schedule_id = sched.id
        ids = {"math": math.id, "a": csci_a.id, "b": csci_b.id}
    return db_manager, schedule_id, ids


def _section_of(session, sid, course_sections):
    rows = session.exec(select(SectionEnrollment)).all()
    return {e.section_id for e in rows if e.student_id == sid} & set(course_sections)


def test_gap_student_moves_to_adjacent_sibling(gap_case):
    db_manager, schedule_id, ids = gap_case
    with db_manager.get_session() as session:
        result = rebalance_seats(session, schedule_id)
        assert result.moves == 1
        assert result.students_improved == 1
        assert result.mean_badness_after < result.mean_badness_before
        assert _section_of(session, "S1", [ids["a"], ids["b"]]) == {ids["b"]}


def test_full_sibling_blocks_move(gap_case):
    db_manager, schedule_id, ids = gap_case
    with db_manager.get_session() as session:
        b = session.get(Section, ids["b"])
        _enroll(session, b, *[f"F{i}" for i in range(b.capacity)])
        result = rebalance_seats(session, schedule_id)
        assert result.moves == 0
        assert _section_of(session, "S1", [ids["a"], ids["b"]]) == {ids["a"]}


def test_time_conflict_blocks_move(db_manager):
    """The only sibling with seats collides with the student's other class."""
    with db_manager.get_session() as session:
        _slot(session, 1, "09:00", "09:50")
        _slot(session, 2, "15:00", "15:50")
        sched = ScheduleVersion(name="T", created_at=datetime.fromisoformat("2026-01-01T00:00:00"),
                                semester="2026FA", status="draft",
                                solver_stats={})
        session.add(sched)
        session.commit()
        session.refresh(sched)
        math = _section(session, sched.id, "20001", "MATH101", "001", 1)
        csci_a = _section(session, sched.id, "20002", "CSCI101", "001", 2)
        # Sibling B meets exactly when MATH does.
        csci_b = _section(session, sched.id, "20003", "CSCI101", "002", 1)
        _student(session, "S1")
        _enroll(session, math, "S1")
        _enroll(session, csci_a, "S1")

        result = rebalance_seats(session, sched.id)
        assert result.moves == 0
        assert _section_of(session, "S1", [csci_a.id, csci_b.id]) == {csci_a.id}


def test_spread_preference_diverges_from_balanced(db_manager):
    """A marathon-packed student moves only under the spread dial."""
    with db_manager.get_session() as session:
        _slot(session, 1, "09:00", "09:50")
        _slot(session, 2, "10:00", "10:50")
        _slot(session, 3, "11:00", "11:50")
        _slot(session, 4, "14:00", "14:50")
        sched = ScheduleVersion(name="T", created_at=datetime.fromisoformat("2026-01-01T00:00:00"),
                                semester="2026FA", status="draft",
                                solver_stats={})
        session.add(sched)
        session.commit()
        session.refresh(sched)
        a = _section(session, sched.id, "20001", "AAA101", "001", 1)
        b = _section(session, sched.id, "20002", "BBB101", "001", 2)
        c1 = _section(session, sched.id, "20003", "CCC101", "001", 3)
        c2 = _section(session, sched.id, "20004", "CCC101", "002", 4)
        _student(session, "S1", pacing="spread")
        for sec in (a, b, c1):
            _enroll(session, sec, "S1")
        schedule_id = sched.id

        # Balanced default would KEEP the tight 9-12 block (no gap, 3 days
        # unchanged); S1's own "spread" preference breaks the marathon by
        # moving CCC101 to 14:00 despite the gap it creates.
        result = rebalance_seats(session, schedule_id, default_mode="balanced")
        assert result.moves == 1
        assert _section_of(session, "S1", [c1.id, c2.id]) == {c2.id}


def test_balanced_student_keeps_tight_block(db_manager):
    """Same layout, no preference: the tight morning block stays."""
    with db_manager.get_session() as session:
        _slot(session, 1, "09:00", "09:50")
        _slot(session, 2, "10:00", "10:50")
        _slot(session, 3, "11:00", "11:50")
        _slot(session, 4, "14:00", "14:50")
        sched = ScheduleVersion(name="T", created_at=datetime.fromisoformat("2026-01-01T00:00:00"),
                                semester="2026FA", status="draft",
                                solver_stats={})
        session.add(sched)
        session.commit()
        session.refresh(sched)
        a = _section(session, sched.id, "20001", "AAA101", "001", 1)
        b = _section(session, sched.id, "20002", "BBB101", "001", 2)
        c1 = _section(session, sched.id, "20003", "CCC101", "001", 3)
        c2 = _section(session, sched.id, "20004", "CCC101", "002", 4)
        _student(session, "S1")  # no preference -> balanced
        for sec in (a, b, c1):
            _enroll(session, sec, "S1")

        result = rebalance_seats(session, sched.id, default_mode="balanced")
        assert result.moves == 0
        assert _section_of(session, "S1", [c1.id, c2.id]) == {c1.id}


def test_unknown_mode_and_schedule(db_manager):
    with db_manager.get_session() as session:
        with pytest.raises(ValueError):
            rebalance_seats(session, 1, default_mode="chaotic")
        with pytest.raises(ValueError):
            rebalance_seats(session, 99999)


def test_api_endpoint(gap_case):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes.schedule import get_session, router

    db_manager, schedule_id, ids = gap_case
    app = FastAPI()
    app.include_router(router)

    def override_session():
        with db_manager.get_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)
    resp = client.post(f"/schedule/{schedule_id}/rebalance?mode=balanced")
    assert resp.status_code == 200
    body = resp.json()
    assert body["moves"] == 1
    assert body["mean_badness_after"] < body["mean_badness_before"]
    assert client.post("/schedule/99999/rebalance").status_code == 404
    assert client.post(
        f"/schedule/{schedule_id}/rebalance?mode=chaotic"
    ).status_code == 422
