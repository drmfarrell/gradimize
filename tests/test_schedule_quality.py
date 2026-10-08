"""Student schedule-quality report: per-student gaps, marathons, day-spread,
and the department prime-time fairness readout."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.core.schedule_quality import analyze_student_quality
from src.core.timeutil import time_to_minutes
from src.database.connection import DatabaseManager
from src.database.models import (
    Course,
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


def _slot(id_, days, start, end):
    return TimeSlot(
        id=id_,
        name=f"{''.join(days)}_{start.replace(':', '')}",
        pattern="".join(days),
        days=days,
        start_time=start,
        end_time=end,
        duration_minutes=time_to_minutes(end) - time_to_minutes(start),
        slot_type="TEST",
    )


def _course(code):
    return Course(
        code=code, title=code, credits=3,
        structure_type=StructureType.STANDALONE,
        required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75", "TEST"],
    )


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
    """One schedule, four enrolled students:

    - SGAP: 9am + 3pm MWF classes -> one huge daily gap, repeated 3 days.
    - SCOMPACT: two back-to-back MWF classes (10-min passing period) ->
      no gap, one fused marathon.
    - S5DAY: one MWF class + one TR class -> on campus all 5 weekdays.
    - SHEAVY: three fused MWF classes -> a 3-class heavy day AND a
      marathon stretch over 3h, on each of 3 days (boundary case for
      "3+ marathons over 3h").

    Plus five course-code prefixes (BIOL/CSCI/ENGL/HIST/PHIL) with
    deliberately varied prime-time placement, including one section
    (PHIL301) with no enrollments at all -- department prime-time is
    enrollment-independent.
    """
    with db_manager.get_session() as session:
        session.add(_slot(1, ["M", "W", "F"], "09:00", "09:50"))  # A: non-prime
        session.add(_slot(2, ["M", "W", "F"], "15:00", "15:50"))  # B: non-prime
        session.add(_slot(3, ["M", "W", "F"], "10:00", "10:50"))  # C: prime
        session.add(_slot(4, ["T", "R"], "09:00", "10:15"))       # D: non-prime
        session.add(_slot(5, ["M", "W", "F"], "11:00", "11:50"))  # E: prime
        session.add(_slot(6, ["M", "W", "F"], "09:00", "10:20"))  # F: non-prime
        session.add(_slot(7, ["M", "W", "F"], "10:25", "11:45"))  # G: prime
        session.add(_slot(8, ["M", "W", "F"], "11:50", "13:10"))  # H: prime
        for code in (
            "CSCI101", "CSCI201", "ENGL101", "ENGL201", "BIOL101", "BIOL201",
            "HIST101", "HIST201", "HIST301", "PHIL301",
        ):
            session.add(_course(code))
        sched = ScheduleVersion(name="Test", created_at=datetime(2026, 1, 1),
                                semester="2026FA", status="draft",
                                solver_stats={})
        session.add(sched)
        session.commit()
        session.refresh(sched)

        # SGAP: 9am + 3pm, same MWF days -> huge gap.
        gap_a = _section(session, sched.id, "20001", "CSCI101", "001", 1)
        gap_b = _section(session, sched.id, "20002", "CSCI201", "001", 2)
        _enroll(session, gap_a, "SGAP")
        _enroll(session, gap_b, "SGAP")

        # SCOMPACT: back-to-back MWF (10-minute passing period, <15 -> fused).
        cmp_a = _section(session, sched.id, "20003", "ENGL101", "001", 1)
        cmp_b = _section(session, sched.id, "20004", "ENGL201", "001", 3)
        _enroll(session, cmp_a, "SCOMPACT")
        _enroll(session, cmp_b, "SCOMPACT")

        # S5DAY: one MWF + one TR -> all 5 weekdays.
        five_a = _section(session, sched.id, "20005", "BIOL101", "001", 1)
        five_b = _section(session, sched.id, "20006", "BIOL201", "001", 4)
        _enroll(session, five_a, "S5DAY")
        _enroll(session, five_b, "S5DAY")

        # SHEAVY: three fused MWF classes -> heavy day + marathon over 3h.
        hv_a = _section(session, sched.id, "20007", "HIST101", "001", 6)
        hv_b = _section(session, sched.id, "20008", "HIST201", "001", 7)
        hv_c = _section(session, sched.id, "20009", "HIST301", "001", 8)
        _enroll(session, hv_a, "SHEAVY")
        _enroll(session, hv_b, "SHEAVY")
        _enroll(session, hv_c, "SHEAVY")

        # PHIL301: no enrollments -- exists purely for a clean 100%-prime dept.
        _section(session, sched.id, "20010", "PHIL301", "001", 5)

        schedule_id = sched.id
    return db_manager, schedule_id


def _by_id(report):
    return {sq.student_id: sq for sq in report.worst_students}


def test_per_student_metrics(seeded):
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        report = analyze_student_quality(session, schedule_id)
    by_id = _by_id(report)

    gap = by_id["SGAP"]
    assert gap.days_on_campus == 3
    assert gap.total_gap_minutes == 930  # 310-minute gap x 3 days (M, W, F)
    assert gap.longest_gap_minutes == 310
    assert gap.max_marathon_minutes == 50  # each class isolated by the big gap
    assert gap.marathons_over_3h == 0
    assert gap.heavy_days == 0
    assert gap.earliest_start == "09:00"
    assert gap.latest_end == "15:50"
    assert gap.badness == 15.5  # 930/60 + 0 + 0

    compact = by_id["SCOMPACT"]
    assert compact.days_on_campus == 3
    assert compact.total_gap_minutes == 0
    assert compact.longest_gap_minutes == 0
    assert compact.max_marathon_minutes == 110  # 09:00-10:50 fused
    assert compact.marathons_over_3h == 0
    assert compact.heavy_days == 0
    assert compact.earliest_start == "09:00"
    assert compact.latest_end == "10:50"
    assert compact.badness == 0.0

    five = by_id["S5DAY"]
    assert five.days_on_campus == 5
    assert five.total_gap_minutes == 0
    assert five.longest_gap_minutes == 0
    assert five.max_marathon_minutes == 75  # the TR meeting is the longest single class
    assert five.marathons_over_3h == 0
    assert five.heavy_days == 0
    assert five.earliest_start == "09:00"
    assert five.latest_end == "10:15"
    assert five.badness == 120.0  # 0 + 60*(5-3) + 0

    heavy = by_id["SHEAVY"]
    assert heavy.days_on_campus == 3
    assert heavy.total_gap_minutes == 0
    assert heavy.longest_gap_minutes == 0
    assert heavy.max_marathon_minutes == 250  # 09:00-13:10 fused
    assert heavy.marathons_over_3h == 3  # once per day, on M, W, F
    assert heavy.heavy_days == 3  # 3 classes, on M, W, F
    assert heavy.earliest_start == "09:00"
    assert heavy.latest_end == "13:10"
    assert heavy.badness == pytest.approx(70 / 60)  # 0 + 0 + (250-180)/60


def test_worst_ranking_order(seeded):
    """Day-spread dominates the badness score by design (see module
    docstring): S5DAY (120) > SGAP (15.5) > SHEAVY (~1.17) > SCOMPACT (0)."""
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        report = analyze_student_quality(session, schedule_id)
    assert [sq.student_id for sq in report.worst_students] == [
        "S5DAY", "SGAP", "SHEAVY", "SCOMPACT",
    ]


def test_distribution_summary(seeded):
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        report = analyze_student_quality(session, schedule_id)
    assert report.total_students == 4
    assert report.mean_days_on_campus == pytest.approx(3.5)  # (3+3+5+3)/4
    assert report.median_days_on_campus == pytest.approx(3.0)
    assert report.mean_gap_minutes == pytest.approx(232.5)  # (930+0+0+0)/4
    assert report.students_5_day == 1  # S5DAY
    assert report.students_big_gap == 1  # SGAP
    assert report.students_heavy_marathon == 1  # SHEAVY


def test_department_prime_time(seeded):
    db_manager, schedule_id = seeded
    with db_manager.get_session() as session:
        report = analyze_student_quality(session, schedule_id)
    by_dept = {d.department: d for d in report.department_prime_time}

    assert by_dept["BIOL"].total_meetings == 2
    assert by_dept["BIOL"].prime_meetings == 0
    assert by_dept["BIOL"].prime_share == 0.0

    assert by_dept["CSCI"].total_meetings == 2
    assert by_dept["CSCI"].prime_meetings == 0
    assert by_dept["CSCI"].prime_share == 0.0

    assert by_dept["ENGL"].total_meetings == 2
    assert by_dept["ENGL"].prime_meetings == 1
    assert by_dept["ENGL"].prime_share == 0.5

    assert by_dept["HIST"].total_meetings == 3
    assert by_dept["HIST"].prime_meetings == 2
    assert by_dept["HIST"].prime_share == pytest.approx(2 / 3)

    # No enrollments at all -- prime-time is computed from the schedule's
    # sections, not from who is enrolled in them.
    assert by_dept["PHIL"].total_meetings == 1
    assert by_dept["PHIL"].prime_meetings == 1
    assert by_dept["PHIL"].prime_share == 1.0

    assert [d.department for d in report.department_prime_time] == [
        "BIOL", "CSCI", "ENGL", "HIST", "PHIL",
    ]


def test_unknown_schedule_raises(seeded):
    db_manager, _ = seeded
    with db_manager.get_session() as session:
        with pytest.raises(ValueError):
            analyze_student_quality(session, 99999)


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
    resp = client.get(f"/schedule/{schedule_id}/student-quality")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_students"] == 4
    assert {s["student_id"] for s in body["worst_students"]} == {
        "SGAP", "SCOMPACT", "S5DAY", "SHEAVY",
    }
    assert any(
        d["department"] == "PHIL" and d["prime_share"] == 1.0
        for d in body["department_prime_time"]
    )
    assert client.get("/schedule/99999/student-quality").status_code == 404


# ─────────────────────────────────────────────────────────────────
# Lunch-break + workday-span metrics (definitions per UniTime's
# conflict taxonomy: no 30-min free block 11:00-13:00; >6h span)
# ─────────────────────────────────────────────────────────────────


def test_lunch_crunched_definitions():
    from src.core.schedule_quality import _lunch_crunched

    h = lambda hh, mm=0: hh * 60 + mm  # noqa: E731
    # Back-to-back classes leave only 10-minute slivers → crunched.
    assert _lunch_crunched(
        [(h(10, 30), h(11, 20)), (h(11, 30), h(12, 20)), (h(12, 30), h(13, 20))]
    ) is True
    # One late-morning class leaves 70 free minutes → fine.
    assert _lunch_crunched([(h(11), h(11, 50))]) is False
    # No classes touching the window at all → fine.
    assert _lunch_crunched([(h(8), h(8, 50)), (h(14), h(15))]) is False
    # Exactly 30 free minutes → fine (threshold is "at least 30").
    assert _lunch_crunched([(h(11), h(11, 30)), (h(12), h(13))]) is False
    # 29 free minutes → crunched.
    assert _lunch_crunched([(h(11), h(11, 31)), (h(12), h(13))]) is True


def test_workday_span_and_lunch_in_analyze():
    from src.core.schedule_quality import analyze_student

    slots = {
        101: _slot(101, ["M"], "08:00", "08:50"),
        102: _slot(102, ["M"], "14:30", "15:20"),
    }
    meetings = {
        1: [SectionMeeting(section_id=1, meeting_sequence=0,
                           time_slot_id=101, room_id="R1")],
        2: [SectionMeeting(section_id=2, meeting_sequence=0,
                           time_slot_id=102, room_id="R1")],
    }
    q = analyze_student("S1", [1, 2], meetings, slots)
    assert q.longest_workday_minutes == (15 * 60 + 20) - 8 * 60  # 440
    assert q.long_workdays == 1
    assert q.lunch_crunch_days == 0


def test_report_counts_lunch_and_workday_students(db_manager):
    with db_manager.get_session() as session:
        session.add(ScheduleVersion(id=1, name="Q", semester="2026FA",
                                    status="draft",
                                    created_at=datetime.fromisoformat("2026-07-20T00:00:00")))
        session.add(_course("CHEM101"))
        session.add(_course("MATH101"))
        # Slot wall across the whole lunch window.
        session.add(_slot(201, ["M"], "11:00", "13:10"))
        session.add(_slot(202, ["M"], "08:00", "08:50"))
        session.add(_slot(203, ["M"], "15:00", "15:50"))
        session.commit()
        _section(session, 1, "90001", "CHEM101", "001", 201)
        _section(session, 1, "90002", "MATH101", "001", 202)
        _section(session, 1, "90003", "MATH101", "002", 203)
        session.commit()
        from sqlmodel import select

        secs = {s.crn: s.id for s in session.exec(
            select(Section).where(Section.schedule_id == 1)
        ).all()}
        session.add(SectionEnrollment(section_id=secs["90001"], student_id="S1"))
        session.add(SectionEnrollment(section_id=secs["90002"], student_id="S2"))
        session.add(SectionEnrollment(section_id=secs["90003"], student_id="S2"))
        session.commit()

        report = analyze_student_quality(session, 1)
    assert report.students_no_lunch == 1      # S1's 11:00-13:10 wall
    assert report.students_long_workday == 1  # S2's 08:00 -> 15:50 span
