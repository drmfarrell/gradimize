"""Canary + structure tests for the student-free schedule report.

scripts/run_report.py is one of the two artifacts an AI assistant is allowed
to read when real data is present (the other being run logs), so it gets the
same treatment as the run logs: plant sentinel student data, generate the
report, and assert no sentinel appears — while the useful section-level
content demonstrably does.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

from src.database.models import (
    ClassStanding,
    Course,
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

CANARY_SID = "ZZ9SID777"
CANARY_NAME = "ZZ9NAME REPORTS"


def _load_run_report():
    spec = importlib.util.spec_from_file_location(
        "run_report",
        Path(__file__).parent.parent / "scripts" / "run_report.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_report"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seeded(db_manager):
    """A tiny solved-looking schedule with one canary student enrolled."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF",
                             days=["M", "W", "F"], start_time="09:00",
                             end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="SCI-104", building="SCI", number="104",
                         capacity=30, room_type=RoomType.LECTURE,
                         equipment=[], is_active=True))
        session.add(Room(id="SCI-999", building="SCI", number="999",
                         capacity=30, room_type=RoomType.LECTURE,
                         equipment=[], is_active=True))  # idle room
        session.add(Course(code="BIO101", title="Intro Bio", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Student(
            id=CANARY_SID, name=CANARY_NAME, email="zz9@example.com",
            class_standing=ClassStanding.FR, credits_earned=0,
            major="BIO", catalog_year=2026,
        ))
        session.add(StudentDemand(
            student_id=CANARY_SID, course_code="BIO101", urgency_score=5.0,
            is_required=True, is_graduation_critical=False,
            reason="Required (test)",
        ))
        session.add(StudentDemand(
            student_id=CANARY_SID, course_code="CHEM101", urgency_score=3.0,
            is_required=False, is_graduation_critical=False,
            reason="Elective (test)",
        ))  # demand with no section → unmet row
        sv = ScheduleVersion(name="Canary Schedule", created_at=datetime(2026, 7, 29),
                             semester="2026FA", status="feasible",
                             solver_stats={"status": "feasible",
                                           "objective_value": 123.0,
                                           "solve_time_seconds": 1.5,
                                           "optimality_gap_pct": 4.2})
        session.add(sv)
        session.commit()
        session.refresh(sv)
        section = Section(schedule_id=sv.id, crn="90001",
                          course_code="BIO101", section_number="001",
                          capacity=30, faculty_id=None)
        session.add(section)
        session.commit()
        session.refresh(section)
        session.add(SectionMeeting(section_id=section.id, meeting_sequence=0,
                                   time_slot_id=1, room_id="SCI-104"))
        session.add(SectionEnrollment(section_id=section.id,
                                      student_id=CANARY_SID,
                                      is_confirmed=True))
        session.commit()
        yield session, sv.id


def test_report_contains_no_student_data(seeded):
    session, schedule_id = seeded
    report = _load_run_report().build_report(session, schedule_id)
    assert CANARY_SID not in report
    assert "ZZ9" not in report
    assert "REPORTS" not in report  # fragment of the canary name


def test_report_contains_the_useful_skeleton(seeded):
    session, schedule_id = seeded
    report = _load_run_report().build_report(session, schedule_id)
    # Grid: course, slot, room, enrollment COUNT.
    assert "BIO101" in report
    assert "MWF_0900" in report
    assert "SCI-104" in report
    assert "1/30" in report
    # Solver stats surfaced.
    assert "gap=4.2%" in report
    # Demand coverage: CHEM101 has 1 demand, no section → unmet 1.
    assert "| CHEM101 | 1 | 0 | 0 | 0 | 1" in report
    assert "Total unmet demand: 1" in report
    # Idle room surfaced.
    assert "SCI-999" in report


def test_report_never_imports_student_tables():
    """The module keeps student-table imports out by design — a diff that
    adds one is a policy change and must trip a test, not sneak by."""
    import re

    src = (Path(__file__).parent.parent / "scripts" / "run_report.py").read_text()
    m = re.search(r"from src\.database\.models import \(([^)]*)\)", src)
    assert m, "expected a parenthesized models import"
    imported = {name.strip().rstrip(",") for name in m.group(1).split()}
    assert "Student" not in imported
    assert "StudentCourseHistory" not in imported
    assert "StudentDemand" in imported  # aggregates over it are allowed


def test_unknown_schedule_raises(seeded):
    session, _ = seeded
    with pytest.raises(ValueError):
        _load_run_report().build_report(session, 99999)
