"""Section viability: cancellation impact for below-minimum sections."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.core.viability import analyze_schedule_viability
from src.database.connection import DatabaseManager
from src.database.models import (
    Course,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    StructureType,
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


def _slot(i, start):
    return TimeSlot(id=i, name=f"MWF_{start.replace(':', '')}", pattern="MWF",
                    days=["M", "W", "F"], start_time=start,
                    end_time=f"{int(start[:2])}:50", duration_minutes=50,
                    slot_type="MWF_50")


def _course(code, **kw):
    return Course(code=code, title=code, credits=3,
                  structure_type=StructureType.STANDALONE,
                  required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                  **kw)


def _section(session, schedule_id, crn, code, number, slot_id, capacity=30):
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


@pytest.fixture
def seeded(db_manager):
    """One schedule: a below-min CSCI101 section with a reseatable sibling,
    a sibling-less biennial course, an empty section, and a healthy one."""
    with db_manager.get_session() as session:
        for i, start in ((1, "09:00"), (2, "10:00"), (3, "11:00")):
            session.add(_slot(i, start))
        session.add(_course("CSCI101", min_enrollment=5))
        session.add(_course("MATH101", min_enrollment=1))
        session.add(_course("BIOL301", min_enrollment=5, cadence="BIENNIAL_EVEN"))
        session.add(_course("EMPTY101", min_enrollment=5))
        sched = ScheduleVersion(name="Test", created_at=datetime(2026, 1, 1),
                                semester="2026FA", status="draft",
                                solver_stats={})
        session.add(sched)
        session.commit()
        session.refresh(sched)

        # CSCI101: section A (below min, slot 1) + sibling B (slot 2, space).
        a = _section(session, sched.id, "10001", "CSCI101", "001", 1)
        b = _section(session, sched.id, "10002", "CSCI101", "002", 2)
        _enroll(session, a, "S1", "S2", "S3")
        _enroll(session, b, "S9")

        # S1 also takes MATH101 in slot 2 — moving S1 to B would collide.
        m = _section(session, sched.id, "10003", "MATH101", "001", 2)
        _enroll(session, m, "S1")

        # BIOL301: lone biennial section, 2 students, nowhere to reseat.
        bio = _section(session, sched.id, "10004", "BIOL301", "001", 3)
        _enroll(session, bio, "S4", "S5")

        # Empty + healthy sections.
        _section(session, sched.id, "10005", "EMPTY101", "001", 3)
        healthy = _section(session, sched.id, "10006", "MATH101", "002", 1)
        _enroll(session, healthy, "S6")

        # S1's CSCI101 need and S4's BIOL301 need are graduation-critical.
        session.add(StudentDemand(student_id="S1", course_code="CSCI101",
                                  urgency_score=225.0, is_required=True,
                                  is_graduation_critical=True, reason="t"))
        session.add(StudentDemand(student_id="S4", course_code="BIOL301",
                                  urgency_score=225.0, is_required=True,
                                  is_graduation_critical=True, reason="t"))
        session.commit()
        schedule_id = sched.id
    return db_manager, schedule_id


def _by_crn(report):
    return {sv.crn: sv for sv in report.below_min}


def test_report_shape_and_selection(seeded):
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        report = analyze_schedule_viability(session, schedule_id)
    assert report.semester == "2026FA"
    assert report.total_sections == 6
    crns = set(_by_crn(report))
    # 10002 (the 1-student sibling) is legitimately below CSCI101's min too.
    assert crns == {"10001", "10002", "10004", "10005"}, (
        "healthy sections must not be flagged"
    )


def test_conflict_aware_reseating(seeded):
    """S2/S3 reseat into the sibling; S1 is blocked by a time conflict."""
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        sv = _by_crn(analyze_schedule_viability(session, schedule_id))["10001"]
    assert sv.enrolled == 3
    assert sv.reseatable == 2
    assert sv.stranded == 1
    assert sv.stranded_students == ["S1"]
    assert sv.stranded_grad_critical == 1
    assert any(alt["crn"] == "10002" for alt in sv.alternatives)
    assert "Do not cancel" in sv.recommendation


def test_biennial_next_offering(seeded):
    """No siblings: everyone stranded; next offering skips odd years."""
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        sv = _by_crn(analyze_schedule_viability(session, schedule_id))["10004"]
    assert sv.stranded == 2
    assert sv.stranded_grad_critical == 1
    assert sv.alternatives == []
    assert sv.next_offering == "2028SP", (
        "BIENNIAL_EVEN after 2026FA must skip 2027SP/2027FA"
    )
    assert "2028SP" in sv.recommendation


def test_empty_section_is_safe(seeded):
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        sv = _by_crn(analyze_schedule_viability(session, schedule_id))["10005"]
    assert sv.enrolled == 0
    assert sv.recommendation == "Empty section — safe to cancel."


def test_unknown_schedule_raises(seeded):
    db_manager, _ = seeded
    with db_manager.get_session() as session:
        with pytest.raises(ValueError):
            analyze_schedule_viability(session, 99999)


def test_api_endpoint(seeded):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes.schedule import get_session, router

    db_manager, schedule_id = seeded
    app = FastAPI()
    app.include_router(router)

    def override_session():
        with db_manager.get_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)
    resp = client.get(f"/schedule/{schedule_id}/viability")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_sections"] == 6
    assert {s["crn"] for s in body["below_min"]} == {
        "10001", "10002", "10004", "10005"
    }
    assert client.get("/schedule/99999/viability").status_code == 404
