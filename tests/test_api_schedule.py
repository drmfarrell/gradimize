"""API tests for schedule routes: CSV exports, pin contract, delete cascade."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from src.api.routes.schedule import get_session, router
from src.database.connection import DatabaseManager
from src.database.models import (
    Course,
    Faculty,
    FinalExamAssignment,
    FinalExamBlock,
    FinalsSchedule,
    Room,
    RoomType,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
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


@pytest.fixture
def seeded(db_manager):
    """Schedule 1 with one section (meeting in slot 1/room R1), enrollment,
    and a finals schedule hanging off it."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                             start_time="08:00", end_time="08:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="R1", building="Main", number="101", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Course(code="CSCI101", title="Intro", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="Prof A", email="a@test.com", max_credits=12))
        session.add(ScheduleVersion(id=1, name="Test Sched", semester="2026FA",
                                    status="draft", created_at=datetime(2026, 7, 14)))
        session.add(Section(id=1, schedule_id=1, crn="10001", course_code="CSCI101",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(SectionMeeting(section_id=1, meeting_sequence=0,
                                   time_slot_id=1, room_id="R1"))
        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        session.add(FinalsSchedule(id=1, schedule_id=1, name="Finals",
                                   created_at=datetime.fromisoformat("2026-07-14T00:00:00")))
        session.add(FinalExamBlock(id=1, finals_schedule_id=1, day="T", ordinal=0,
                                   start_time="08:30", end_time="10:20",
                                   label="Tue 8:30-10:20"))
        session.add(FinalExamAssignment(finals_schedule_id=1, section_id=1,
                                        block_id=1, room_id="R1"))
        session.commit()
    return db_manager


@pytest.fixture
def client(seeded):
    app = FastAPI()
    app.include_router(router)

    def override_session():
        with seeded.get_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def test_export_banner_is_csv_download(client):
    resp = client.get("/schedule/1/export/banner")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert resp.text.splitlines()[0].startswith("CRN,")


def test_export_enrollments_is_csv_download(client):
    resp = client.get("/schedule/1/export/enrollments")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")


def test_pin_with_slot_only_defaults_room_from_meeting(client, seeded):
    """A slot-only body must actually move the meeting (old code no-op'd)."""
    resp = client.patch(
        "/schedule/1/sections/1/pin",
        json={"time_slot_id": 2, "pin_reason": "test move"},
    )
    assert resp.status_code == 200

    with seeded.get_session() as session:
        m0 = session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id == 1)
        ).one()
        assert m0.time_slot_id == 2, "slot must move"
        assert m0.room_id == "R1", "room defaults from the existing meeting"
        assert session.get(Section, 1).is_pinned is True


def test_pin_partial_without_meeting_is_422(client, seeded):
    """No meeting-0 to default from -> reject, don't silently pin-in-place."""
    with seeded.get_session() as session:
        session.add(Section(id=2, schedule_id=1, crn="10002", course_code="CSCI101",
                            section_number="002", capacity=30))
        session.commit()

    resp = client.patch(
        "/schedule/1/sections/2/pin",
        json={"time_slot_id": 2, "pin_reason": "test"},
    )
    assert resp.status_code == 422


def test_pin_without_placement_pins_in_place(client, seeded):
    resp = client.patch("/schedule/1/sections/1/pin", json={"pin_reason": "keep"})
    assert resp.status_code == 200
    with seeded.get_session() as session:
        m0 = session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id == 1)
        ).one()
        assert m0.time_slot_id == 1, "pin-in-place must not move the meeting"
        assert session.get(Section, 1).is_pinned is True


def test_delete_schedule_cascades_to_finals(client, seeded):
    resp = client.delete("/schedule/1")
    assert resp.status_code == 200

    with seeded.get_session() as session:
        assert session.get(ScheduleVersion, 1) is None
        assert session.exec(select(Section)).all() == []
        assert session.exec(select(SectionMeeting)).all() == []
        assert session.exec(select(SectionEnrollment)).all() == []
        assert session.exec(select(FinalsSchedule)).all() == []
        assert session.exec(select(FinalExamBlock)).all() == []
        assert session.exec(select(FinalExamAssignment)).all() == []


def test_diff_schedules(client, seeded):
    """Diff: section moved to a new slot in schedule 2, plus one added."""
    with seeded.get_session() as session:
        session.add(ScheduleVersion(id=2, name="V2", semester="2026FA",
                                    status="draft", created_at=datetime(2026, 7, 15)))
        # same (course, section) key, different slot -> "changed"
        session.add(Section(id=10, schedule_id=2, crn="20001", course_code="CSCI101",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(SectionMeeting(section_id=10, meeting_sequence=0,
                                   time_slot_id=2, room_id="R1"))
        # new section only in v2 -> "added"
        session.add(Section(id=11, schedule_id=2, crn="20002", course_code="CSCI101",
                            section_number="002", capacity=30))
        session.commit()

    resp = client.get("/schedule/2/diff/1")
    assert resp.status_code == 200
    d = resp.json()
    assert d["schedule"]["id"] == 2 and d["base"]["id"] == 1
    assert [a["section_number"] for a in d["added"]] == ["002"]
    assert d["removed"] == []
    assert len(d["changed"]) == 1
    ch = d["changed"][0]
    assert ch["changes"]["slot"] == {"from": "MWF_0800", "to": "MWF_0900"}
    assert "room" not in ch["changes"]


def test_explain_section(client):
    resp = client.get("/schedule/1/sections/1/explain")
    assert resp.status_code == 200
    reasons = resp.json()["reasons"]
    assert reasons, "must return at least one reason"
    text = " ".join(reasons)
    assert "LECTURE" in text          # room-type constraint explained
    assert "Prof A" in text           # instructor explained
    assert "Seats 1/30" in text       # enrollment vs capacity


# ─────────────────────────────────────────────────────────────────
# iCal download endpoints (thin wrappers over src/core/ical.py)
# ─────────────────────────────────────────────────────────────────


def test_ical_instructor_is_calendar_download(client):
    resp = client.get("/schedule/1/ical/instructor/F1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    assert ".ics" in resp.headers["content-disposition"]
    assert "BEGIN:VCALENDAR" in resp.text
    assert "BEGIN:VEVENT" in resp.text


def test_ical_room_is_calendar_download(client):
    resp = client.get("/schedule/1/ical/room/R1")
    assert resp.status_code == 200
    assert "BEGIN:VEVENT" in resp.text


def test_ical_unknown_student_404s(client):
    # SectionEnrollment references S1 but no Student row exists in the seed.
    resp = client.get("/schedule/1/ical/student/S1")
    assert resp.status_code == 404
