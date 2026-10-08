"""Tests for bulk student-schedule export (src/core/distribution.py)."""

import csv
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.distribution import export_student_schedules
from src.database.connection import DatabaseManager
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
def seeded_db(db_manager):
    """One schedule: MATH100 (meets, has instructor), BME100 (no meeting)."""
    with db_manager.get_session() as session:
        slot = TimeSlot(
            id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
            start_time="08:00", end_time="08:50", duration_minutes=50,
            slot_type="MWF_50",
        )
        session.add(slot)

        room = Room(id="SCI-101", building="Science", number="101", capacity=30,
                    room_type=RoomType.LECTURE)
        session.add(room)

        fac = Faculty(id="FSMITH01", name="S. Smith", email="smith@test.edu")
        session.add(fac)

        session.add(Course(
            code="MATH100", title="Precalculus", credits=3,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
        ))
        session.add(Course(
            code="BME100", title="Biomed Intro", credits=3,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
        ))

        schedule = ScheduleVersion(
            id=1, name="Fall26", created_at=datetime.fromisoformat("2026-01-01T00:00:00"),
            semester="2026FA", status="published",
        )
        session.add(schedule)

        sec1 = Section(id=1, schedule_id=1, crn="C1", course_code="MATH100",
                        section_number="001", capacity=30, faculty_id="FSMITH01")
        sec2 = Section(id=2, schedule_id=1, crn="C2", course_code="BME100",
                        section_number="001", capacity=30)  # no meeting, no faculty
        session.add(sec1)
        session.add(sec2)
        session.add(SectionMeeting(section_id=1, meeting_sequence=0, time_slot_id=1,
                                    room_id="SCI-101"))
        # sec2: no meeting row.

        session.add(Student(id="S1", name="Alice", email="s1@test.edu",
                             class_standing=ClassStanding.SO, credits_earned=60,
                             major="MATH", catalog_year=2024))
        session.add(Student(id="S2", name="Bob", email="s2@test.edu",
                             class_standing=ClassStanding.FR, credits_earned=0,
                             major="BME", catalog_year=2026))

        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        session.add(SectionEnrollment(section_id=2, student_id="S2"))
        session.commit()

    return db_manager


def _read_csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_export_writes_expected_csv_rows(seeded_db, tmp_path):
    with seeded_db.get_session() as session:
        stats = export_student_schedules(session, 1, tmp_path)

    csv_path = tmp_path / "student_schedules.csv"
    assert csv_path.exists()
    rows = _read_csv_rows(csv_path)
    assert len(rows) == 2

    row_by_student = {r["student_id"]: r for r in rows}
    r1 = row_by_student["S1"]
    assert r1["course_code"] == "MATH100"
    assert r1["course_title"] == "Precalculus"
    assert r1["crn"] == "C1"
    assert r1["section_number"] == "001"
    assert r1["meeting_days"] == "MWF"
    assert r1["start_time"] == "08:00"
    assert r1["end_time"] == "08:50"
    assert r1["room_id"] == "SCI-101"
    assert r1["instructor_name"] == "S. Smith"

    # BME100 section has zero meetings -> one row, meeting/room fields blank,
    # instructor blank (unset).
    r2 = row_by_student["S2"]
    assert r2["course_code"] == "BME100"
    assert r2["meeting_days"] == ""
    assert r2["start_time"] == ""
    assert r2["end_time"] == ""
    assert r2["room_id"] == ""
    assert r2["instructor_name"] == ""

    assert stats == {
        "students": 2,
        "enrollments": 2,
        "csv_rows": 2,
        "sheets_written": 2,
        "ics_written": 2,
        "sections_without_meetings": 1,
    }


def test_export_writes_sheets_and_ical_per_student(seeded_db, tmp_path):
    with seeded_db.get_session() as session:
        export_student_schedules(session, 1, tmp_path)

    assert (tmp_path / "sheets" / "S1.md").exists()
    assert (tmp_path / "sheets" / "S2.md").exists()
    assert (tmp_path / "ical" / "S1.ics").exists()
    assert (tmp_path / "ical" / "S2.ics").exists()
    sheet_text = (tmp_path / "sheets" / "S1.md").read_text()
    assert "MATH100" in sheet_text
    ics_text = (tmp_path / "ical" / "S1.ics").read_text()
    assert "BEGIN:VCALENDAR" in ics_text


def test_no_sheets_flag_suppresses_sheets(seeded_db, tmp_path):
    with seeded_db.get_session() as session:
        stats = export_student_schedules(session, 1, tmp_path, include_sheets=False)

    assert stats["sheets_written"] == 0
    assert not (tmp_path / "sheets").exists()
    assert (tmp_path / "ical").exists()


def test_no_ical_flag_suppresses_ical(seeded_db, tmp_path):
    with seeded_db.get_session() as session:
        stats = export_student_schedules(session, 1, tmp_path, include_ical=False)

    assert stats["ics_written"] == 0
    assert not (tmp_path / "ical").exists()
    assert (tmp_path / "sheets").exists()


def test_deterministic_double_run_identical_bytes(seeded_db, tmp_path):
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    with seeded_db.get_session() as session:
        export_student_schedules(session, 1, out1)
        export_student_schedules(session, 1, out2)

    b1 = (out1 / "student_schedules.csv").read_bytes()
    b2 = (out2 / "student_schedules.csv").read_bytes()
    assert b1 == b2


def test_unknown_schedule_raises(seeded_db, tmp_path):
    with seeded_db.get_session() as session:
        with pytest.raises(ValueError):
            export_student_schedules(session, 999, tmp_path)


def test_export_does_not_mutate_db(seeded_db, tmp_path):
    with seeded_db.get_session() as session:
        export_student_schedules(session, 1, tmp_path)

    with seeded_db.get_session() as session:
        enrollments = list(session.exec(select(SectionEnrollment)).all())
        sections = list(session.exec(select(Section)).all())
        assert len(enrollments) == 2
        assert len(sections) == 2
