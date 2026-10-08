"""Integration tests for the 2026-10 formulation changes.

- exact student sectioning (phase B) removes the student time overlaps the
  joint model is blind to;
- sibling symmetry breaking orders interchangeable sections and keeps the
  model feasible (with and without the flag);
- the time-point clique no-overlap formulation agrees with the pairwise one.
"""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.core.validate import check_schedule_conflicts
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
    ScheduleVersion,
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


def _slot(i, pattern, days, start, end, slot_type="MWF_50"):
    return TimeSlot(
        id=i, name=f"{pattern}_{start.replace(':', '')}", pattern=pattern, days=days,
        start_time=start, end_time=end, duration_minutes=50, slot_type=slot_type,
    )


def _course(code):
    return Course(
        code=code, title=code, credits=3, structure_type=StructureType.STANDALONE,
        required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
    )


def _room(rid, room_type=RoomType.LECTURE, number="1"):
    return Room(id=rid, building="B", number=number, capacity=30, room_type=room_type)


def _faculty(fid, courses, max_credits=12):
    return Faculty(id=fid, name=fid, email=f"{fid}@t", max_credits=max_credits,
                   approved_courses=courses)


def _student(i):
    return Student(
        id=f"S{i}", name=f"Student {i}", email=f"s{i}@test.com",
        class_standing=ClassStanding.SO, credits_earned=30, major="CSCI", catalog_year=2024,
    )


@pytest.fixture
def one_slot_two_courses(db_manager):
    """Only ONE time slot exists, so both courses must meet at the same time.

    The joint model happily seats S1 in both (it has no per-student overlap
    rule); exact sectioning must keep only one seat.
    """
    with db_manager.get_session() as session:
        session.add(_slot(1, "MWF", ["M", "W", "F"], "08:00", "08:50"))
        session.add(_room("R1", RoomType.LECTURE, "1"))
        session.add(_room("R2", RoomType.LECTURE, "2"))
        session.add(_course("AAA101"))
        session.add(_course("BBB101"))
        session.add(_faculty("F1", ["AAA101"], 12))
        session.add(_faculty("F2", ["BBB101"], 12))
        session.add(_student(1))
        session.add(StudentDemand(student_id="S1", course_code="AAA101", urgency_score=10.0,
                                  is_required=True, reason="t"))
        session.add(StudentDemand(student_id="S1", course_code="BBB101", urgency_score=20.0,
                                  is_required=True, reason="t"))
        session.commit()
    return db_manager


def _solve(db, config=None):
    with db.get_session() as session:
        cm = ConflictMatrix(session)
        cm.build_matrix()
        config = config or SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30
        config.solver_settings["num_workers"] = 2
        engine = SchedulingEngine(session, cm, config)
        sections = engine.create_sections("2026FA")
        result = engine.solve(sections, "t", "2026FA")
        assert result.status in ("optimal", "feasible"), result.status
        sched_id = session.exec(
            select(ScheduleVersion).order_by(ScheduleVersion.id.desc())  # type: ignore[union-attr]
        ).first().id
        report = check_schedule_conflicts(session, sched_id)
        meetings = {
            s.crn: sorted(
                (m.meeting_sequence, m.time_slot_id)
                for m in result.meetings if m.section_id == s.id
            )
            for s in result.sections
        }
        return result, report, meetings


def test_exact_sectioning_removes_student_overlap(one_slot_two_courses):
    result, report, _ = _solve(one_slot_two_courses)
    assert report.student == 0
    assert len(result.enrollments) == 1
    stats = result.stats["sectioning"]
    assert stats["mode"] == "exact"
    assert stats["joint_overlaps_removed"] >= 1, stats
    assert stats["seats_joint"] == 2 and stats["seats"] == 1
    # The higher-urgency seat (BBB101, 20) wins the tie-break.
    kept = {s.id: s.course_code for s in result.sections}[result.enrollments[0].section_id]
    assert kept == "BBB101"


def test_joint_only_mode_still_double_seats(one_slot_two_courses):
    """Documents the pre-phase-B behaviour behind the opt-out flag."""
    config = SchedulerConfig()
    config.solver_settings["exact_sectioning"] = False
    result, report, _ = _solve(one_slot_two_courses, config)
    assert len(result.enrollments) == 2
    assert report.student == 1
    assert result.stats["sectioning"] is None


@pytest.fixture
def three_siblings(db_manager):
    """60 students demand one course -> 3 sibling sections; one room, so the
    siblings need three distinct slots."""
    with db_manager.get_session() as session:
        for i, start in enumerate(["08:00", "09:00", "10:00", "11:00"], start=1):
            end = f"{int(start[:2]):02d}:50"
            session.add(_slot(i, "MWF", ["M", "W", "F"], start, end))
        session.add(_room("R1", RoomType.LECTURE, "1"))
        session.add(_course("AAA101"))
        session.add(_faculty("F1", ["AAA101"], 99))
        for i in range(1, 61):
            session.add(_student(i))
            session.add(StudentDemand(student_id=f"S{i}", course_code="AAA101",
                                      urgency_score=10.0 + i % 7, is_required=True, reason="t"))
        session.commit()
    return db_manager


def test_symmetry_breaking_orders_siblings(three_siblings):
    result, report, meetings = _solve(three_siblings)
    assert len(result.sections) == 3
    assert report.room == 0 and report.student == 0
    by_number = sorted(result.sections, key=lambda s: s.section_number)
    slot_of = [meetings[s.crn][0][1] for s in by_number]
    assert slot_of == sorted(slot_of), slot_of
    assert len(set(slot_of)) == 3  # one room: three distinct slots


def test_symmetry_breaking_can_be_disabled(three_siblings):
    config = SchedulerConfig()
    config.solver_settings["symmetry_breaking"] = False
    result, report, _ = _solve(three_siblings, config)
    assert len(result.sections) == 3
    assert report.room == 0


@pytest.fixture
def lab_shape(db_manager):
    """The March-2026 shape: Mon lab and Wed lab both overlap the MWF slot
    but not each other, so they MAY share a room while the MWF section
    cannot share with either."""
    with db_manager.get_session() as session:
        session.add(_slot(1, "MWF", ["M", "W", "F"], "08:00", "08:50"))
        session.add(TimeSlot(id=19, name="M_LAB", pattern="M", days=["M"], start_time="08:00",
                             end_time="10:30", duration_minutes=150, slot_type="LAB_150"))
        session.add(TimeSlot(id=25, name="W_LAB", pattern="W", days=["W"], start_time="08:00",
                             end_time="10:30", duration_minutes=150, slot_type="LAB_150"))
        session.add(_room("L1", RoomType.LAB_BIO, "1"))
        session.add(_room("R1", RoomType.LECTURE, "2"))
        for code in ("LAB1", "LAB2"):
            session.add(Course(
                code=code, title="l", credits=1, structure_type=StructureType.STANDALONE,
                required_room_types=["LAB_BIO"], valid_slot_types=["LAB_150"],
            ))
        session.add(_course("LEC1"))
        session.add(_faculty("F1", ["LAB1", "LAB2"], 99))
        session.add(_faculty("F2", ["LEC1"], 99))
        session.add(_student(1))
        session.add(_student(2))
        for code, sid in (("LAB1", "S1"), ("LAB2", "S2"), ("LEC1", "S1")):
            session.add(StudentDemand(student_id=sid, course_code=code, urgency_score=10.0,
                                      is_required=True, reason="t"))
        session.commit()
    return db_manager


@pytest.mark.parametrize("formulation", ["cliques", "pairwise"])
def test_no_overlap_formulations_agree_on_lab_shape(lab_shape, formulation):
    config = SchedulerConfig()
    config.solver_settings["no_overlap_formulation"] = formulation
    result, report, meetings = _solve(lab_shape, config)
    assert len(result.sections) == 3
    assert report.room == 0 and report.faculty == 0
    lab_slots = sorted(
        meetings[s.crn][0][1] for s in result.sections if s.course_code.startswith("LAB")
    )
    assert lab_slots == [19, 25]  # the two labs share room L1 on different days
