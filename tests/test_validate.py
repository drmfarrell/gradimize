"""Tests for validate.check_schedule_conflicts — and a pin of the student-
overlap SEMANTICS: the solver treats student overlap as a soft penalty (not
a hard constraint), so overlapping enrollments can exist and the validator
is the tool that surfaces them."""

from datetime import datetime

import pytest

from src.core.validate import check_schedule_conflicts
from src.database.models import (
    Course,
    Faculty,
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
def conflicted_schedule(db_manager):
    """Two sections in the SAME slot: same room, same faculty, one shared student."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="R1", building="Main", number="101", capacity=30,
                         room_type=RoomType.LECTURE))
        for code in ("BIO101", "CHM101"):
            session.add(Course(code=code, title=code, credits=3,
                               structure_type=StructureType.STANDALONE))
        session.add(Faculty(id="F1", name="Prof A", email="a@t.co", max_credits=12))
        session.add(ScheduleVersion(id=1, name="S", semester="2026FA",
                                    status="draft", created_at=datetime(2026, 7, 14)))
        session.add(Section(id=1, schedule_id=1, crn="10001", course_code="BIO101",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(Section(id=2, schedule_id=1, crn="10002", course_code="CHM101",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(SectionMeeting(section_id=1, meeting_sequence=0, time_slot_id=1,
                                   room_id="R1"))
        session.add(SectionMeeting(section_id=2, meeting_sequence=0, time_slot_id=1,
                                   room_id="R1"))
        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        session.add(SectionEnrollment(section_id=2, student_id="S1"))
        session.commit()
    return db_manager


def test_checker_reports_all_three_conflict_kinds(conflicted_schedule):
    with conflicted_schedule.get_session() as session:
        report = check_schedule_conflicts(session, 1)

        assert report.room >= 1, "same room + same slot must be reported"
        assert report.faculty >= 1, "same faculty + same slot must be reported"
        assert report.student >= 1, (
            "overlapping student enrollment must be reported — student overlap "
            "is a soft solver penalty by design, and this checker is what "
            "surfaces the residual cases"
        )
        assert report.total == len(report.conflicts)


def test_checker_clean_schedule_reports_nothing(db_manager):
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(TimeSlot(id=2, name="TR_1000", pattern="TR", days=["T", "R"],
                             start_time="10:00", end_time="11:15", duration_minutes=75,
                             slot_type="TR_75"))
        session.add(Room(id="R1", building="Main", number="101", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Course(code="BIO101", title="B", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Course(code="CHM101", title="C", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Faculty(id="F1", name="Prof A", email="a@t.co", max_credits=12))
        session.add(ScheduleVersion(id=1, name="S", semester="2026FA",
                                    status="draft", created_at=datetime(2026, 7, 14)))
        session.add(Section(id=1, schedule_id=1, crn="10001", course_code="BIO101",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(Section(id=2, schedule_id=1, crn="10002", course_code="CHM101",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(SectionMeeting(section_id=1, meeting_sequence=0, time_slot_id=1,
                                   room_id="R1"))
        session.add(SectionMeeting(section_id=2, meeting_sequence=0, time_slot_id=2,
                                   room_id="R1"))
        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        session.add(SectionEnrollment(section_id=2, student_id="S1"))
        session.commit()

        report = check_schedule_conflicts(session, 1)
        assert report.total == 0


def _pack_schedule(db_manager, group):
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="R1", building="Main", number="101", capacity=30,
                         room_type=RoomType.LECTURE))
        for code in ("HIST200", "AFAM200"):
            session.add(Course(code=code, title=code, credits=3, crosslist_group=group,
                               structure_type=StructureType.STANDALONE))
        session.add(Faculty(id="F1", name="Prof A", email="a@t.co", max_credits=12))
        session.add(ScheduleVersion(id=1, name="S", semester="2026FA",
                                    status="draft", created_at=datetime(2026, 7, 14)))
        for i, code in ((1, "HIST200"), (2, "AFAM200")):
            session.add(Section(id=i, schedule_id=1, crn=f"1000{i}", course_code=code,
                                section_number="001", capacity=30, faculty_id="F1"))
            session.add(SectionMeeting(section_id=i, meeting_sequence=0, time_slot_id=1,
                                       room_id="R1"))
        session.commit()
        return check_schedule_conflicts(session, 1)


def test_crosslist_pack_is_not_a_conflict(db_manager):
    report = _pack_schedule(db_manager, "XL1")
    assert report.room == 0 and report.faculty == 0


def test_non_crosslisted_pair_is_still_a_conflict(db_manager):
    report = _pack_schedule(db_manager, None)
    assert report.room == 1 and report.faculty == 1
