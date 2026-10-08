"""Tests for the absorption test (Fall 2026 back-test, experiment B)."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.absorption import absorption_score, compare_absorption
from src.database.connection import DatabaseManager
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
    """One schedule, three courses, sections with known capacities/slots, demands."""
    with db_manager.get_session() as session:
        # Two non-overlapping MWF slots, one that overlaps slot A directly.
        slot_a = TimeSlot(
            id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
            start_time="08:00", end_time="08:50", duration_minutes=50,
            slot_type="MWF_50",
        )
        slot_b = TimeSlot(
            id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
            start_time="09:00", end_time="09:50", duration_minutes=50,
            slot_type="MWF_50",
        )
        for s in (slot_a, slot_b):
            session.add(s)

        room = Room(id="SCI-101", building="Science", number="101", capacity=30,
                    room_type=RoomType.LECTURE)
        session.add(room)

        for code, title in (
            ("MATH100", "Precalculus"), ("CSCI100", "Intro CS"),
            ("BME100", "Biomed Intro"),
        ):
            session.add(Course(
                code=code, title=title, credits=3,
                structure_type=StructureType.STANDALONE,
                required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
            ))

        schedule = ScheduleVersion(
            id=1, name="Manual", created_at=datetime(2026, 1, 1), semester="2026FA",
            status="imported",
        )
        session.add(schedule)

        # MATH100: Sec1 capacity 1 @ slot A; Sec2 capacity 5 @ slot B (no overlap).
        sec1 = Section(id=1, schedule_id=1, crn="C1", course_code="MATH100",
                        section_number="001", capacity=1)
        sec2 = Section(id=2, schedule_id=1, crn="C2", course_code="MATH100",
                        section_number="002", capacity=5)
        # CSCI100: Sec3 capacity 5 @ slot A (overlaps Sec1's slot directly).
        sec3 = Section(id=3, schedule_id=1, crn="C3", course_code="CSCI100",
                        section_number="001", capacity=5)
        # BME100: Sec4 capacity 5, NO meeting (unmatched at import).
        sec4 = Section(id=4, schedule_id=1, crn="C4", course_code="BME100",
                        section_number="001", capacity=5)
        for s in (sec1, sec2, sec3, sec4):
            session.add(s)
        session.add(
            SectionMeeting(
                section_id=1, meeting_sequence=0, time_slot_id=1, room_id="SCI-101"
            )
        )
        session.add(
            SectionMeeting(
                section_id=2, meeting_sequence=0, time_slot_id=2, room_id="SCI-101"
            )
        )
        session.add(
            SectionMeeting(
                section_id=3, meeting_sequence=0, time_slot_id=1, room_id="SCI-101"
            )
        )
        # sec4: no meeting row.

        # Students: S1 (SO, high urgency, grad-critical MATH100), S2 (SO),
        # S3 (FR) with BME100 demand.
        session.add(Student(id="S1", name="S1", email="s1@test.edu",
                             class_standing=ClassStanding.SO, credits_earned=60,
                             major="MATH", catalog_year=2024))
        session.add(Student(id="S2", name="S2", email="s2@test.edu",
                             class_standing=ClassStanding.SO, credits_earned=60,
                             major="MATH", catalog_year=2024))
        session.add(Student(id="S3", name="S3", email="s3@test.edu",
                             class_standing=ClassStanding.FR, credits_earned=0,
                             major="BME", catalog_year=2026))

        # S1: grad-critical MATH100 (high urgency, seated first) + CSCI100
        # (same slot as MATH100 Sec1 -> must not double-seat) + a duplicate
        # MATH100 demand row (one-section-per-course check).
        session.add(StudentDemand(
            student_id="S1", course_code="MATH100", urgency_score=50.0,
            is_required=True, is_graduation_critical=True, reason="Major req",
        ))
        session.add(StudentDemand(
            student_id="S1", course_code="MATH100", urgency_score=50.0,
            is_required=True, is_graduation_critical=True, reason="dup row",
        ))
        session.add(StudentDemand(
            student_id="S1", course_code="CSCI100", urgency_score=10.0,
            is_required=False, is_graduation_critical=False, reason="Elective",
        ))
        # S2: MATH100 only, lower urgency than S1 -> gets Sec2 once Sec1 is full.
        session.add(StudentDemand(
            student_id="S2", course_code="MATH100", urgency_score=5.0,
            is_required=True, is_graduation_critical=False, reason="Major req",
        ))
        # S3 (FR): BME100, seatable despite unplaced section.
        session.add(StudentDemand(
            student_id="S3", course_code="BME100", urgency_score=8.0,
            is_required=True, is_graduation_critical=False, reason="Major req",
        ))

        session.commit()

    return db_manager


def test_capacity_and_overlap_and_metrics(seeded_db):
    with seeded_db.get_session() as session:
        result = absorption_score(session, schedule_id=1)

    # 5 demand rows total (incl. one duplicate MATH100 row for S1).
    assert result["total_demand"] == 5

    # S1 seated into MATH100 Sec1 (highest urgency, grad-critical), duplicate
    # row also counts seated (one-section-per-course), CSCI100 Sec3 blocked
    # by the slot-A overlap with Sec1 -> unmet. S2 pushed to Sec2 (capacity
    # respected: Sec1 was full). S3 seated into the unplaced BME100 section.
    assert result["seated"] == 4  # MATH100 x2 (S1), MATH100 (S2), BME100 (S3)
    assert result["seated_pct"] == 80.0

    assert result["grad_critical_total"] == 2
    assert result["grad_critical_seated"] == 2
    assert result["grad_critical_pct"] == 100.0

    assert result["fr_demand_total"] == 1
    assert result["fr_seated"] == 1
    assert result["fr_pct"] == 100.0

    assert result["distinct_students_total"] == 3
    # S1 has an unmet CSCI100 demand -> not fully seated. S2, S3 fully seated.
    assert result["distinct_students_fully_seated"] == 2

    assert result["total_seats_offered"] == 1 + 5 + 5 + 5
    assert result["unplaced_sections"] == 1

    assert result["top_unmet_courses"] == [{"course_code": "CSCI100", "unmet": 1}]


def test_overlap_prevents_double_seating(seeded_db):
    """CSCI100 Sec3 has free seats but shares slot A with S1's MATH100 seat."""
    with seeded_db.get_session() as session:
        result = absorption_score(session, schedule_id=1)
    unmet_courses = {u["course_code"] for u in result["top_unmet_courses"]}
    assert "CSCI100" in unmet_courses


def test_scoring_does_not_mutate_db(seeded_db):
    with seeded_db.get_session() as session:
        absorption_score(session, schedule_id=1)

    with seeded_db.get_session() as session:
        assert list(session.exec(select(SectionEnrollment)).all()) == []
        demands = list(session.exec(select(StudentDemand)).all())
        assert len(demands) == 5
        sections = {s.id: s.capacity for s in session.exec(select(Section)).all()}
        assert sections == {1: 1, 2: 5, 3: 5, 4: 5}


def test_two_schedules_scored_independently(seeded_db):
    with seeded_db.get_session() as session:
        # Second schedule: same courses, MATH100 offered with generous capacity
        # (no overlap with CSCI100) -> should absorb everything.
        schedule2 = ScheduleVersion(
            id=2, name="Optimizer", created_at=datetime.fromisoformat("2026-01-02T00:00:00"),
            semester="2026FA", status="draft",
        )
        session.add(schedule2)
        sec5 = Section(id=5, schedule_id=2, crn="D1", course_code="MATH100",
                        section_number="001", capacity=5)
        sec6 = Section(id=6, schedule_id=2, crn="D2", course_code="CSCI100",
                        section_number="001", capacity=5)
        sec7 = Section(id=7, schedule_id=2, crn="D3", course_code="BME100",
                        section_number="001", capacity=5)
        session.add_all([sec5, sec6, sec7])
        session.add(
            SectionMeeting(
                section_id=5, meeting_sequence=0, time_slot_id=1, room_id="SCI-101"
            )
        )
        session.add(
            SectionMeeting(
                section_id=6, meeting_sequence=0, time_slot_id=2, room_id="SCI-101"
            )
        )
        session.add(
            SectionMeeting(
                section_id=7, meeting_sequence=0, time_slot_id=2, room_id="SCI-101"
            )
        )
        session.commit()

        rows = compare_absorption(session, [1, 2])

    assert rows[0]["schedule_id"] == 1
    assert rows[1]["schedule_id"] == 2
    assert rows[0]["seated"] == 4
    assert rows[1]["seated"] == 5  # all demand rows seated, no overlap this time
    assert rows[1]["unplaced_sections"] == 0
    assert rows[1]["top_unmet_courses"] == []


def test_unknown_schedule_id_raises(seeded_db):
    with seeded_db.get_session() as session:
        with pytest.raises(ValueError):
            absorption_score(session, schedule_id=999)


def test_crosslisted_sections_pool_capacity(db_manager):
    """HIST210/AFAM210 share crosslist_group XL07 and one 40-seat room:
    their 30+30 CRN capacities must pool to 40 seats, not 60 — otherwise
    the manual baseline's absorption metrics are inflated for exactly the
    courses most likely to be cross-listed."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(
            id=1, name="MWF_1130", pattern="MWF", days=["M", "W", "F"],
            start_time="11:30", end_time="12:20", duration_minutes=50,
            slot_type="MWF_50",
        ))
        session.add(Room(id="HALL-102", building="HALL", number="102",
                         capacity=40, room_type=RoomType.LECTURE))
        for code, title in (("HIST210", "African-American History"),
                            ("AFAM210", "African-American History (AFAM)")):
            session.add(Course(
                code=code, title=title, credits=3,
                structure_type=StructureType.STANDALONE,
                required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                crosslist_group="XL07",
            ))
        session.add(ScheduleVersion(
            id=1, name="Manual", created_at=datetime.fromisoformat("2026-01-01T00:00:00"),
            semester="2026FA", status="imported",
        ))
        session.add(Section(id=1, schedule_id=1, crn="20815",
                            course_code="HIST210", section_number="001",
                            capacity=30))
        session.add(Section(id=2, schedule_id=1, crn="20816",
                            course_code="AFAM210", section_number="001",
                            capacity=30))
        session.add(SectionMeeting(section_id=1, meeting_sequence=0,
                                   time_slot_id=1, room_id="HALL-102"))
        session.add(SectionMeeting(section_id=2, meeting_sequence=0,
                                   time_slot_id=1, room_id="HALL-102"))

        # 45 demands across the pair: only 40 can physically fit the room.
        for i in range(25):
            session.add(Student(id=f"H{i}", name=f"H{i}", email="",
                                class_standing=ClassStanding.SO,
                                credits_earned=30, major="HIST",
                                catalog_year=2024))
            session.add(StudentDemand(student_id=f"H{i}", course_code="HIST210",
                                      urgency_score=10.0, is_required=True,
                                      is_graduation_critical=False, reason="t"))
        for i in range(20):
            session.add(Student(id=f"A{i}", name=f"A{i}", email="",
                                class_standing=ClassStanding.SO,
                                credits_earned=30, major="AFAM",
                                catalog_year=2024))
            session.add(StudentDemand(student_id=f"A{i}", course_code="AFAM210",
                                      urgency_score=10.0, is_required=True,
                                      is_graduation_critical=False, reason="t"))
        session.commit()

        result = absorption_score(session, schedule_id=1)

    assert result["total_demand"] == 45
    assert result["crosslist_packs"] == 1
    assert result["total_seats_offered"] == 40  # min(30+30, room 40)
    assert result["seated"] == 40
    assert result["seated_pct"] == round(100.0 * 40 / 45, 1)
