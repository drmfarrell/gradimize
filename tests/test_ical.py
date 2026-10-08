"""Tests for src/core/ical.py: .ics calendar export per instructor/room/
student. Seeds ScheduleVersion/Section/SectionMeeting/SectionEnrollment
rows directly — no solver run, test_viability.py/test_offerings.py style."""

from datetime import date, datetime, timedelta

import pytest

from src.core.ical import (
    default_term_dates,
    ics_for_instructor,
    ics_for_room,
    ics_for_student,
)
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

# 2026-08-24 is a Monday and 2026-12-11 is a Friday — chosen so DTSTART for
# an MWF slot lands exactly on TERM_START (easy to assert precisely) and
# UNTIL is a plain, unambiguous date.
TERM_START = date(2026, 8, 24)
TERM_END = date(2026, 12, 11)


def _mwf_slot(id=1, start="09:00", end="09:50"):
    return TimeSlot(id=id, name=f"MWF_{start.replace(':', '')}", pattern="MWF",
                     days=["M", "W", "F"], start_time=start, end_time=end,
                     duration_minutes=50, slot_type="MWF_50")


def _tr_slot(id=2, start="09:30", end="10:45"):
    return TimeSlot(id=id, name=f"TR_{start.replace(':', '')}", pattern="TR",
                     days=["T", "R"], start_time=start, end_time=end,
                     duration_minutes=75, slot_type="TR_75")


def _room(id, building="Science Hall", number=None, capacity=30):
    return Room(id=id, building=building, number=number or id, capacity=capacity,
                room_type=RoomType.LECTURE)


def _faculty(id, name=None):
    return Faculty(id=id, name=name or id, email=f"{id.lower()}@example.edu")


def _student(id, name=None):
    return Student(id=id, name=name or id, email=f"{id.lower()}@example.edu",
                   class_standing=ClassStanding.JR, credits_earned=60,
                   major="UNDE", catalog_year=2024)


def _course(code, title=None):
    return Course(code=code, title=title or code, credits=3,
                  structure_type=StructureType.STANDALONE)


def _schedule(id=1, semester="2026FA"):
    return ScheduleVersion(id=id, name="Test", semester=semester, status="draft",
                            created_at=datetime.fromisoformat("2026-01-01T00:00:00"))


def _section(id, schedule_id, crn, code, number, faculty_id=None, capacity=30):
    return Section(id=id, schedule_id=schedule_id, crn=crn, course_code=code,
                   section_number=number, capacity=capacity, faculty_id=faculty_id)


def _meeting(id, section_id, seq, slot_id, room_id):
    return SectionMeeting(id=id, section_id=section_id, meeting_sequence=seq,
                          time_slot_id=slot_id, room_id=room_id)


@pytest.fixture
def seeded(db_manager):
    """One schedule (id=1, "2026FA"):

    - CSCI101-001 (F1, MWF slot, room R1) — S1 enrolled.
    - CSCI102-002 (F2, TR slot, room R2)  — S2 enrolled.
    - BIOL301-001 (no faculty, TWO meetings, both in room R1: an MWF
      meeting and a TR meeting) — exercises multi-meeting -> multi-VEVENT
      and lets the room-R1 calendar test assert both faculty-CSCI101 and
      BIOL301's meetings show up while CSCI102 (room R2) does not.
    """
    with db_manager.get_session() as session:
        session.add(_mwf_slot(1))
        session.add(_tr_slot(2))
        session.add(_room("R1"))
        session.add(_room("R2", building="Annex"))
        session.add(_faculty("F1", name="J. Smith"))
        session.add(_faculty("F2", name="A. Jones"))
        session.add(_student("S1", name="Pat Lee"))
        session.add(_student("S2", name="Sam Kim"))
        session.add(_course("CSCI101", title="Intro to Programming"))
        session.add(_course("CSCI102", title="Data Structures"))
        session.add(_course("BIOL301", title="Genetics"))
        session.add(_schedule(1, "2026FA"))
        session.commit()

        session.add(_section(1, 1, "10001", "CSCI101", "001", faculty_id="F1"))
        session.add(_section(2, 1, "10002", "CSCI102", "002", faculty_id="F2"))
        session.add(_section(3, 1, "10003", "BIOL301", "001", faculty_id=None))
        session.commit()

        session.add(_meeting(1, 1, 0, 1, "R1"))  # CSCI101: MWF in R1
        session.add(_meeting(2, 2, 0, 2, "R2"))  # CSCI102: TR in R2
        session.add(_meeting(3, 3, 0, 1, "R1"))  # BIOL301 meeting 0: MWF in R1
        session.add(_meeting(4, 3, 1, 2, "R1"))  # BIOL301 meeting 1: TR in R1
        session.commit()

        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        session.add(SectionEnrollment(section_id=2, student_id="S2"))
        session.commit()

    return db_manager


# ─────────────────────────────────────────────────────────────────
# (1) Instructor calendar
# ─────────────────────────────────────────────────────────────────


def test_instructor_calendar_scopes_to_that_instructor_and_shapes_events(seeded):
    with seeded.get_session() as session:
        ics = ics_for_instructor(
            session, 1, "F1", term_start=TERM_START, term_end=TERM_END
        )

    assert "10001" in ics  # F1's own section
    assert "10002" not in ics  # F2's section must not appear
    assert "10003" not in ics  # unassigned BIOL301 must not appear
    assert ics.count("BEGIN:VEVENT") == 1

    # MWF slot starting exactly on TERM_START (a Monday).
    assert f"DTSTART:{TERM_START.strftime('%Y%m%d')}T090000" in ics
    assert f"DTEND:{TERM_START.strftime('%Y%m%d')}T095000" in ics
    assert "BYDAY=MO,WE,FR" in ics
    assert f"UNTIL={TERM_END.strftime('%Y%m%d')}T235959" in ics

    assert "UID:gradimize-1-10001-m0@gradimize" in ics
    assert "SUMMARY:CSCI101-001 Intro to Programming" in ics
    assert "LOCATION:Science Hall R1" in ics
    # Cheap enrolled-headcount lookup: S1 is enrolled in section 1.
    assert "Enrolled: 1" in ics


def test_instructor_calendar_falls_back_to_default_term_dates(seeded):
    """No term_start/term_end passed -> derived from schedule.semester."""
    with seeded.get_session() as session:
        ics = ics_for_instructor(session, 1, "F1")

    fall_start, fall_end = default_term_dates("2026FA")
    assert f"UNTIL={fall_end.strftime('%Y%m%d')}T235959" in ics
    assert "DTSTART:" in ics
    # DTSTART must fall within one week of the default term start.
    dtstart_line = next(
        line for line in ics.split("\r\n") if line.startswith("DTSTART:")
    )
    dtstart_date = date(
        int(dtstart_line[8:12]), int(dtstart_line[12:14]), int(dtstart_line[14:16])
    )
    assert fall_start <= dtstart_date <= fall_start + timedelta(days=6)


# ─────────────────────────────────────────────────────────────────
# (2) Student calendar
# ─────────────────────────────────────────────────────────────────


def test_student_calendar_has_only_enrolled_sections(seeded):
    with seeded.get_session() as session:
        ics = ics_for_student(
            session, 1, "S1", term_start=TERM_START, term_end=TERM_END
        )

    assert "10001" in ics  # S1 is enrolled in this one
    assert "10002" not in ics  # S1 is NOT enrolled in this one
    assert "10003" not in ics
    assert ics.count("BEGIN:VEVENT") == 1
    assert "Instructor: J. Smith" in ics  # student calendars name the instructor


def test_student_with_no_enrollments_gets_empty_calendar(seeded):
    with seeded.get_session() as session:
        session.add(_student("S3", name="No Classes"))
        session.commit()
        ics = ics_for_student(
            session, 1, "S3", term_start=TERM_START, term_end=TERM_END
        )
    assert ics.count("BEGIN:VEVENT") == 0
    assert "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics


# ─────────────────────────────────────────────────────────────────
# (3) Room calendar + multi-meeting sections
# ─────────────────────────────────────────────────────────────────


def test_room_calendar_scopes_to_room_and_multi_meeting_yields_two_vevents(seeded):
    with seeded.get_session() as session:
        ics_r1 = ics_for_room(
            session, 1, "R1", term_start=TERM_START, term_end=TERM_END
        )
        ics_r2 = ics_for_room(
            session, 1, "R2", term_start=TERM_START, term_end=TERM_END
        )

    # R1: CSCI101 (1 meeting) + BIOL301 (2 meetings) = 3 VEVENTs.
    assert ics_r1.count("BEGIN:VEVENT") == 3
    assert "10001" in ics_r1
    assert "10003" in ics_r1
    assert "10002" not in ics_r1  # CSCI102 meets only in R2
    assert ics_r1.count("UID:gradimize-1-10003-m0@gradimize") == 1
    assert ics_r1.count("UID:gradimize-1-10003-m1@gradimize") == 1

    # R2: only CSCI102's single meeting.
    assert ics_r2.count("BEGIN:VEVENT") == 1
    assert "10002" in ics_r2
    assert "10001" not in ics_r2
    assert "10003" not in ics_r2


def test_section_with_no_meetings_contributes_no_events(db_manager):
    with db_manager.get_session() as session:
        session.add(_faculty("F9", name="No Meetings"))
        session.add(_course("MATH100", title="Ghost Course"))
        session.add(_schedule(1, "2026FA"))
        session.commit()
        session.add(_section(1, 1, "40001", "MATH100", "001", faculty_id="F9"))
        session.commit()

        ics = ics_for_instructor(
            session, 1, "F9", term_start=TERM_START, term_end=TERM_END
        )

    assert ics.count("BEGIN:VEVENT") == 0
    assert "BEGIN:VCALENDAR" in ics
    assert "END:VCALENDAR" in ics


# ─────────────────────────────────────────────────────────────────
# (4) Escaping
# ─────────────────────────────────────────────────────────────────


def test_comma_in_course_title_is_escaped(db_manager):
    with db_manager.get_session() as session:
        session.add(_mwf_slot(1))
        session.add(_room("R1"))
        session.add(_course("CSCI200", title="Robotics, Advanced"))
        session.add(_schedule(1, "2026FA"))
        session.commit()
        session.add(_section(1, 1, "20001", "CSCI200", "001"))
        session.commit()
        session.add(_meeting(1, 1, 0, 1, "R1"))
        session.commit()

        ics = ics_for_room(
            session, 1, "R1", term_start=TERM_START, term_end=TERM_END
        )

    assert "Robotics\\, Advanced" in ics
    # The raw, unescaped title must not survive anywhere in the output.
    assert "Robotics, Advanced" not in ics


# ─────────────────────────────────────────────────────────────────
# (5) Folding
# ─────────────────────────────────────────────────────────────────


def test_long_title_folds_without_any_line_exceeding_75_octets(db_manager):
    long_title = (
        "Introduction to Advanced Computational Methods in Theoretical "
        "and Applied Biophysics for Interdisciplinary Research Programs "
        "Across Departments and Colleges"
    )
    with db_manager.get_session() as session:
        session.add(_mwf_slot(1))
        session.add(_room("R1"))
        session.add(_course("BIOL999", title=long_title))
        session.add(_schedule(1, "2026FA"))
        session.commit()
        session.add(_section(1, 1, "30001", "BIOL999", "001"))
        session.commit()
        session.add(_meeting(1, 1, 0, 1, "R1"))
        session.commit()

        ics = ics_for_room(
            session, 1, "R1", term_start=TERM_START, term_end=TERM_END
        )

    output_lines = ics.split("\r\n")
    for line in output_lines:
        assert len(line.encode("utf-8")) <= 75, f"line exceeds 75 octets: {line!r}"

    # Folding must actually have happened (the title alone is >75 octets),
    # and continuation lines are marked with exactly one leading space.
    continuation_lines = [line for line in output_lines if line.startswith(" ")]
    assert continuation_lines, "expected at least one folded continuation line"


# ─────────────────────────────────────────────────────────────────
# (6) default_term_dates
# ─────────────────────────────────────────────────────────────────


def test_default_term_dates_fall():
    assert default_term_dates("2026FA") == (date(2026, 8, 25), date(2026, 12, 12))


def test_default_term_dates_spring():
    assert default_term_dates("2027SP") == (date(2027, 1, 20), date(2027, 5, 8))


def test_default_term_dates_summer():
    assert default_term_dates("2026SU") == (date(2026, 6, 1), date(2026, 7, 31))


def test_default_term_dates_raises_on_garbage():
    with pytest.raises(ValueError):
        default_term_dates("not-a-semester")


def test_default_term_dates_raises_on_unsupported_term_code():
    """WINTER parses as a semester elsewhere in the codebase but has no
    default window defined here."""
    with pytest.raises(ValueError):
        default_term_dates("2026WI")


# ─────────────────────────────────────────────────────────────────
# (7) Structural sanity
# ─────────────────────────────────────────────────────────────────


def test_structural_sanity_of_vcalendar_wrapper(seeded):
    with seeded.get_session() as session:
        ics = ics_for_instructor(
            session, 1, "F1", term_start=TERM_START, term_end=TERM_END
        )

    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT")
    stripped = ics.rstrip("\r\n")
    assert stripped.startswith("BEGIN:VCALENDAR")
    assert stripped.endswith("END:VCALENDAR")

    # CRLF line endings throughout; no bare LF outside of a CRLF pair.
    assert "\r\n" in ics
    assert "\n" not in ics.replace("\r\n", "")


# ─────────────────────────────────────────────────────────────────
# Not-found behavior (ValueError for schedule/faculty/room/student)
# ─────────────────────────────────────────────────────────────────


def test_unknown_schedule_raises_value_error(seeded):
    with seeded.get_session() as session:
        with pytest.raises(ValueError):
            ics_for_instructor(session, 99999, "F1")
        with pytest.raises(ValueError):
            ics_for_room(session, 99999, "R1")
        with pytest.raises(ValueError):
            ics_for_student(session, 99999, "S1")


def test_unknown_faculty_raises_value_error(seeded):
    with seeded.get_session() as session:
        with pytest.raises(ValueError):
            ics_for_instructor(session, 1, "NOPE")


def test_unknown_room_raises_value_error(seeded):
    with seeded.get_session() as session:
        with pytest.raises(ValueError):
            ics_for_room(session, 1, "NOPE")


def test_unknown_student_raises_value_error(seeded):
    with seeded.get_session() as session:
        with pytest.raises(ValueError):
            ics_for_student(session, 1, "NOPE")
