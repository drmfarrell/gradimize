"""Advisor mode: student schedule sheets + counterfactual move analysis."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.schedule import router
from src.core.advisor import explain_move, student_sheet
from src.database.connection import DatabaseManager, get_session
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    StructureType,
    Student,
    StudentDemand,
    TimeSlot,
)


@pytest.fixture
def db_manager():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield manager
    db_path.unlink(missing_ok=True)


@pytest.fixture
def seeded(db_manager):
    """Schedule 1: CHEM101 (slot 1, R1, F1) + MATH101 seminar-only (slot 2,
    R3, F2) + PHYS101 (slot 2, R2, F1 — F1's second section). S1 takes CHEM
    (graduation-critical) and MATH; S2 takes CHEM only."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0800", pattern="MWF",
                             days=["M", "W", "F"], start_time="08:00",
                             end_time="08:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(TimeSlot(id=2, name="MWF_0900", pattern="MWF",
                             days=["M", "W", "F"], start_time="09:00",
                             end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(TimeSlot(id=3, name="TR_0900", pattern="TR",
                             days=["T", "R"], start_time="09:00",
                             end_time="10:15", duration_minutes=75,
                             slot_type="TR_75"))
        session.add(Room(id="R1", building="Sci", number="101", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Room(id="R2", building="Sci", number="102", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Room(id="R3", building="Hum", number="201", capacity=30,
                         room_type=RoomType.SEMINAR))
        session.add(Course(code="CHEM101", title="Chemistry", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"]))
        session.add(Course(code="MATH101", title="Mathematics", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["SEMINAR"],
                           valid_slot_types=["MWF_50"]))
        session.add(Course(code="PHYS101", title="Physics", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="Prof One", email="1@t.co",
                            max_credits=12))
        session.add(Faculty(id="F2", name="Prof Two", email="2@t.co",
                            max_credits=12))
        for sid in ("S1", "S2"):
            session.add(Student(id=sid, name=f"Student {sid[-1]}", email="",
                                class_standing=ClassStanding.JR,
                                credits_earned=60, major="CHEM",
                                catalog_year=2024))
        session.add(StudentDemand(student_id="S1", course_code="CHEM101",
                                  urgency_score=30.0, is_required=True,
                                  is_graduation_critical=True, reason="t"))
        session.add(ScheduleVersion(id=1, name="Published", semester="2026FA",
                                    status="approved",
                                    created_at=datetime.fromisoformat("2026-07-20T00:00:00")))
        session.add(Section(id=1, schedule_id=1, crn="10001",
                            course_code="CHEM101", section_number="001",
                            capacity=30, faculty_id="F1"))
        session.add(Section(id=2, schedule_id=1, crn="10002",
                            course_code="MATH101", section_number="001",
                            capacity=30, faculty_id="F2"))
        session.add(Section(id=3, schedule_id=1, crn="10003",
                            course_code="PHYS101", section_number="001",
                            capacity=30, faculty_id="F1"))
        session.add(SectionMeeting(section_id=1, meeting_sequence=0,
                                   time_slot_id=1, room_id="R1"))
        session.add(SectionMeeting(section_id=2, meeting_sequence=0,
                                   time_slot_id=2, room_id="R3"))
        session.add(SectionMeeting(section_id=3, meeting_sequence=0,
                                   time_slot_id=2, room_id="R2"))
        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        session.add(SectionEnrollment(section_id=1, student_id="S2"))
        session.add(SectionEnrollment(section_id=2, student_id="S1"))
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


# ── student sheet ──


def test_sheet_lists_courses_week_and_credits(seeded):
    with seeded.get_session() as session:
        sheet = student_sheet(session, 1, "S1")
    assert "Student 1 (S1)" in sheet
    assert "| CHEM101 |" in sheet and "| MATH101 |" in sheet
    assert "PHYS101" not in sheet  # not enrolled
    assert "**Total credits: 6**" in sheet
    assert "**Monday**" in sheet
    assert "- 08:00–08:50 CHEM101-001 (Sci 101)" in sheet
    assert "- 09:00–09:50 MATH101-001 (Hum 201)" in sheet
    assert "/ical/student/S1" in sheet


def test_sheet_handles_no_enrollments(seeded):
    with seeded.get_session() as session:
        # S2 exists but MATH-less; make a truly empty case with a new student
        session.add(Student(id="S9", name="Empty", email="",
                            class_standing=ClassStanding.FR, credits_earned=0,
                            major="CHEM", catalog_year=2026))
        session.commit()
        sheet = student_sheet(session, 1, "S9")
    assert "No enrollments" in sheet


def test_sheet_route_is_markdown_and_404s(client):
    resp = client.get("/schedule/1/student/S1/sheet")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# Course Schedule" in resp.text
    assert client.get("/schedule/1/student/NOPE/sheet").status_code == 404
    assert client.get("/schedule/9/student/S1/sheet").status_code == 404


# ── explain-move ──


def test_move_blocked_by_instructor_double_booking(seeded):
    """CHEM101 → slot 2: F1 already teaches PHYS101 there; S1's MATH also
    collides (graduation-critical seat at risk)."""
    with seeded.get_session() as session:
        result = explain_move(session, 1, 1, 2)
    assert any("already teaches" in b and "PHYS101-001" in b
               for b in result["blockers"])
    assert result["student_conflicts"]["count"] == 1
    assert result["student_conflicts"]["graduation_critical"] == 1
    assert result["verdict"].startswith("Cannot move")


def test_move_blocked_by_slot_type(seeded):
    with seeded.get_session() as session:
        result = explain_move(session, 1, 1, 3)  # CHEM101 → TR_75
    assert any("slot types" in b and "MWF_50" in b for b in result["blockers"])


def test_move_possible_at_a_price(seeded):
    """MATH101 → slot 1: no hard blocker (R3 frees up, F2 is free) but S1's
    CHEM101 meets then — one conflict, not graduation-critical for MATH."""
    with seeded.get_session() as session:
        result = explain_move(session, 1, 2, 1)
    assert result["blockers"] == []
    assert result["free_rooms"] == ["R3"]
    assert result["student_conflicts"]["count"] == 1
    assert result["student_conflicts"]["graduation_critical"] == 0
    assert result["verdict"].startswith("Possible, at a price")


def test_move_blocked_when_all_compatible_rooms_taken(seeded):
    """Occupy the only seminar room at slot 1 → MATH101 → slot 1 is dead."""
    with seeded.get_session() as session:
        session.add(Course(code="HIST101", title="History", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["SEMINAR"],
                           valid_slot_types=["MWF_50"]))
        session.add(Section(id=4, schedule_id=1, crn="10004",
                            course_code="HIST101", section_number="001",
                            capacity=30, faculty_id="F2"))
        session.add(SectionMeeting(section_id=4, meeting_sequence=0,
                                   time_slot_id=1, room_id="R3"))
        session.commit()
        result = explain_move(session, 1, 2, 1)
    assert any("SEMINAR" in b and "occupied" in b for b in result["blockers"])


def test_move_route_smoke(client):
    resp = client.get("/schedule/1/sections/1/explain-move?slot_id=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_slot"]["name"] == "MWF_0900"
    assert body["verdict"]
    assert client.get(
        "/schedule/1/sections/99/explain-move?slot_id=2"
    ).status_code == 404
