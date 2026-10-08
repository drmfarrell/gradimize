"""Repair mode: minimal-change re-solve anchored to a published schedule.

The contract under test: after a disruption (instructor lost, room lost),
repair_solve moves ONLY what the disruption forces — every other section
keeps its published slot, room, instructor, and roster.
"""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
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
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield manager

    db_path.unlink(missing_ok=True)


def _seed(session, *, chem_only=False):
    """Two disjoint courses with dedicated instructors and separated rooms.

    MATH101 is SEMINAR-only (R3 is the sole seminar room) so room-level
    breakage of a CHEM room can never legitimately touch MATH.
    """
    for i in (1, 2, 3):
        session.add(TimeSlot(id=i, name=f"MWF_{7 + i:02d}00", pattern="MWF",
                             days=["M", "W", "F"], start_time=f"{7 + i:02d}:00",
                             end_time=f"{7 + i:02d}:50", duration_minutes=50,
                             slot_type="MWF_50"))
    for r in ("R1", "R2"):
        session.add(Room(id=r, building="B", number=r[-1], capacity=30,
                         room_type=RoomType.LECTURE))
    session.add(Room(id="R3", building="B", number="3", capacity=30,
                     room_type=RoomType.SEMINAR))
    session.add(Course(code="CHEM101", title="Chem", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                       min_enrollment=2))
    session.add(Faculty(id="F1", name="Chem Prof", email="f1@t.co", max_credits=12,
                        approved_courses=["CHEM101"]))
    if not chem_only:
        session.add(Course(code="MATH101", title="Math", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["SEMINAR"],
                           valid_slot_types=["MWF_50"], min_enrollment=2))
        session.add(Faculty(id="F2", name="Math Prof", email="f2@t.co",
                            max_credits=12, approved_courses=["MATH101"]))
    for i, code in enumerate(
        ["CHEM101"] * 3 + ([] if chem_only else ["MATH101"] * 3)
    ):
        sid = f"S{i + 1}"
        session.add(Student(id=sid, name=f"Student {i + 1}", email="",
                            class_standing=ClassStanding.SO, credits_earned=30,
                            major="CSCI", catalog_year=2024))
        session.add(StudentDemand(student_id=sid, course_code=code,
                                  urgency_score=20.0, is_required=True,
                                  reason="t"))
    session.commit()


def _engine(session):
    cm = ConflictMatrix(session)
    cm.build_matrix()
    config = SchedulerConfig()
    config.solver_settings["max_time_seconds"] = 30
    return SchedulingEngine(session, cm, config)


def _solve_base(session, **create_kwargs):
    engine = _engine(session)
    sections = engine.create_sections("2026FA", **create_kwargs)
    result = engine.solve(sections, "Base", "2026FA")
    assert result.status in ("optimal", "feasible")
    return result.sections[0].schedule_id


def _snapshot(session, schedule_id):
    """(course, section_number) -> (meetings tuple, faculty_id, roster)."""
    out = {}
    sections = session.exec(
        select(Section).where(Section.schedule_id == schedule_id)
    ).all()
    for s in sections:
        meetings = tuple(
            (m.time_slot_id, m.room_id)
            for m in session.exec(
                select(SectionMeeting)
                .where(SectionMeeting.section_id == s.id)
                .order_by(SectionMeeting.meeting_sequence)
            ).all()
        )
        roster = frozenset(
            e.student_id
            for e in session.exec(
                select(SectionEnrollment).where(SectionEnrollment.section_id == s.id)
            ).all()
        )
        out[(s.course_code, s.section_number)] = (meetings, s.faculty_id, roster)
    return out


def test_repair_unchanged_data_reproduces_schedule(db_manager):
    """No disruption → the repaired schedule is placement-for-placement,
    seat-for-seat identical to the base."""
    with db_manager.get_session() as session:
        _seed(session)
        base_id = _solve_base(session)
        base = _snapshot(session, base_id)

        result = _engine(session).repair_solve(base_id)
        assert result.status in ("optimal", "feasible")
        assert result.stats["repair_mode"] is True
        new_id = result.sections[0].schedule_id
        assert new_id != base_id, "repair must write a NEW schedule version"
        assert _snapshot(session, new_id) == base


def test_repair_replaces_only_broken_instructor(db_manager):
    """Instructor becomes unavailable everywhere; a qualified substitute
    exists → only that section's instructor changes. Slot, room, roster,
    and the other course stay exactly as published."""
    with db_manager.get_session() as session:
        _seed(session)
        base_id = _solve_base(session)
        base = _snapshot(session, base_id)
        chem_key = next(k for k in base if k[0] == "CHEM101")
        math_key = next(k for k in base if k[0] == "MATH101")
        assert base[chem_key][1] == "F1"

        f1 = session.get(Faculty, "F1")
        f1.unavailable_slots = [1, 2, 3]  # can't teach at all this term
        session.add(f1)
        session.add(Faculty(id="F3", name="New Hire", email="f3@t.co",
                            max_credits=12, approved_courses=["CHEM101"]))
        session.commit()

        result = _engine(session).repair_solve(base_id)
        assert result.status in ("optimal", "feasible")
        new = _snapshot(session, result.sections[0].schedule_id)

        # CHEM: new instructor, everything else anchored in place.
        assert new[chem_key][1] == "F3"
        assert new[chem_key][0] == base[chem_key][0], "slot/room must not move"
        assert new[chem_key][2] == base[chem_key][2], "roster must not churn"
        # MATH: completely untouched.
        assert new[math_key] == base[math_key]


def test_repair_moves_only_lost_room_section(db_manager):
    """The assigned room goes offline → that section moves to another room,
    keeps its time slot, and nothing else changes."""
    with db_manager.get_session() as session:
        _seed(session)
        base_id = _solve_base(session)
        base = _snapshot(session, base_id)
        chem_key = next(k for k in base if k[0] == "CHEM101")
        math_key = next(k for k in base if k[0] == "MATH101")
        (chem_slot, chem_room), = base[chem_key][0]

        room = session.get(Room, chem_room)
        room.is_active = False
        session.add(room)
        session.commit()

        result = _engine(session).repair_solve(base_id)
        assert result.status in ("optimal", "feasible")
        new = _snapshot(session, result.sections[0].schedule_id)

        (new_slot, new_room), = new[chem_key][0]
        assert new_room != chem_room
        assert new_slot == chem_slot, "time anchor outweighs a room move"
        assert new[chem_key][1] == base[chem_key][1]
        assert new[chem_key][2] == base[chem_key][2]
        assert new[math_key] == base[math_key]


def test_repair_keeps_students_in_their_sibling_sections(db_manager):
    """Two sections of the same course: repair must not reshuffle students
    between siblings (the seat anchor breaks the tie toward stability)."""
    with db_manager.get_session() as session:
        _seed(session, chem_only=True)
        base_id = _solve_base(session, sections_per_course={"CHEM101": 2})
        base = _snapshot(session, base_id)

        result = _engine(session).repair_solve(base_id)
        assert result.status in ("optimal", "feasible")
        assert _snapshot(session, result.sections[0].schedule_id) == base


# ─────────────────────────────────────────────────────────────────
# run_repair (generate-level): emit protocol + repair_diff payload
# ─────────────────────────────────────────────────────────────────


def _recorder():
    events = []

    def emit(event_type, **fields):
        events.append({"type": event_type, **fields})

    return emit, events


def test_run_repair_emits_done_with_diff(db_manager, monkeypatch):
    from src.core import generate as generate_mod

    with db_manager.get_session() as session:
        _seed(session)
        base_id = _solve_base(session)
        f1 = session.get(Faculty, "F1")
        f1.unavailable_slots = [1, 2, 3]
        session.add(f1)
        session.add(Faculty(id="F3", name="New Hire", email="f3@t.co",
                            max_credits=12, approved_courses=["CHEM101"]))
        session.commit()

    monkeypatch.setattr(generate_mod, "get_engine", lambda: db_manager.engine)
    emit, events = _recorder()
    generate_mod.run_repair(emit, base_id, time_limit=30)

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert not [e for e in events if e["type"] == "error"]
    diff = done[0]["repair_diff"]
    assert diff["base"]["id"] == base_id
    assert diff["unchanged_count"] == 1  # MATH101 untouched
    assert len(diff["changed"]) == 1
    change = diff["changed"][0]
    assert change["course_code"] == "CHEM101"
    assert set(change["changes"]) == {"instructor"}
    # Each move is also narrated as a log line for the SSE stream.
    assert any(
        e["type"] == "log" and e["msg"].startswith("MOVED CHEM101-")
        for e in events
    )


def test_run_repair_missing_schedule_emits_single_error(db_manager, monkeypatch):
    from src.core import generate as generate_mod

    with db_manager.get_session() as session:
        _seed(session)
    monkeypatch.setattr(generate_mod, "get_engine", lambda: db_manager.engine)
    emit, events = _recorder()
    generate_mod.run_repair(emit, 99999)
    assert [e["type"] for e in events] == ["error"]
