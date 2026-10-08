"""Tests for the CP-SAT solver."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine, compute_blocked_slot_ids
from src.database.connection import DatabaseManager
from src.database.models import (
    BlockedPeriod,
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
    Section,
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


@pytest.fixture
def small_dataset(db_manager):
    """Create a small test dataset."""
    with db_manager.get_session() as session:
        # Time slots
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                    start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                    start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=3, name="TR_0800", pattern="TR", days=["T", "R"],
                    start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
        ]
        for s in slots:
            session.add(s)

        # Rooms
        rooms = [
            Room(id="R1", building="Test", number="101", capacity=30, room_type=RoomType.LECTURE),
            Room(id="R2", building="Test", number="102", capacity=25, room_type=RoomType.LECTURE),
        ]
        for r in rooms:
            session.add(r)

        # Courses
        courses = [
            Course(code="CSCI101", title="Intro", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]),
            Course(code="CSCI201", title="DS", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]),
        ]
        for c in courses:
            session.add(c)

        # Faculty
        faculty = [
            Faculty(
                id="F1", name="Prof A", email="a@test.com",
                max_credits=12, approved_courses=["CSCI101"],
            ),
            Faculty(
                id="F2", name="Prof B", email="b@test.com",
                max_credits=12, approved_courses=["CSCI201"],
            ),
        ]
        for f in faculty:
            session.add(f)

        # Students
        students = [
            Student(id="S1", name="Student 1", email="s1@test.com",
                   class_standing=ClassStanding.FR, credits_earned=0, major="CSCI",
                   catalog_year=2024),
            Student(id="S2", name="Student 2", email="s2@test.com",
                   class_standing=ClassStanding.SO, credits_earned=30, major="CSCI",
                   catalog_year=2024),
        ]
        for s in students:
            session.add(s)

        # Demands
        demands = [
            StudentDemand(
                student_id="S1", course_code="CSCI101", urgency_score=15.0,
                is_required=True, is_graduation_critical=False, reason="Test",
            ),
            StudentDemand(
                student_id="S2", course_code="CSCI101", urgency_score=25.0,
                is_required=True, is_graduation_critical=False, reason="Test",
            ),
            StudentDemand(
                student_id="S2", course_code="CSCI201", urgency_score=30.0,
                is_required=True, is_graduation_critical=False, reason="Test",
            ),
        ]
        for d in demands:
            session.add(d)

        session.commit()

    return db_manager


def test_section_creation(small_dataset):
    """Test that sections are created based on demand."""
    with small_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        engine = SchedulingEngine(session, conflict_matrix)
        sections = engine.create_sections("2025SP")

        # Should have at least one section per course
        course_codes = {s.course_code for s in sections}
        assert "CSCI101" in course_codes
        assert "CSCI201" in course_codes


def test_solver_finds_solution(small_dataset):
    """Test that solver finds a valid solution."""
    with small_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        assert result.status in ("optimal", "feasible")
        assert len(result.sections) > 0


def test_room_no_double_booking(small_dataset):
    """Test that rooms are not double-booked."""
    with small_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        engine = SchedulingEngine(session, conflict_matrix)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        # Assert first: an INFEASIBLE regression must fail, not no-op the test
        assert result.status in ("optimal", "feasible"), result.status
        # Check for room conflicts via SectionMeeting
        room_slot_pairs = []
        for s in result.sections:
            for m in result.meetings:
                if m.section_id == s.id:
                    pair = (m.room_id, m.time_slot_id)
                    assert pair not in room_slot_pairs, (
                        f"Room {m.room_id} double-booked in slot {m.time_slot_id}"
                    )
                    room_slot_pairs.append(pair)


def test_faculty_load_limits(small_dataset):
    """Test that faculty load limits are respected."""
    with small_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        engine = SchedulingEngine(session, conflict_matrix)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        # Assert first: an INFEASIBLE regression must fail, not no-op the test
        assert result.status in ("optimal", "feasible"), result.status
        # Calculate credits per faculty
        faculty_credits = {}
        for s in result.sections:
            if s.faculty_id:
                course = engine.course_by_code.get(s.course_code)
                if course:
                    faculty_credits[s.faculty_id] = (
                        faculty_credits.get(s.faculty_id, 0) + course.credits
                    )

        # Check against limits
        for f in engine.faculty:
            assigned = faculty_credits.get(f.id, 0)
            assert assigned <= f.max_credits, (
                f"Faculty {f.id} overloaded: {assigned} > {f.max_credits}"
            )


@pytest.fixture
def graduation_critical_dataset(db_manager):
    """Create a dataset with graduation-critical demands."""
    with db_manager.get_session() as session:
        # Time slots
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                    start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                    start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
        ]
        for s in slots:
            session.add(s)

        # Rooms
        rooms = [
            Room(id="R1", building="Test", number="101", capacity=30, room_type=RoomType.LECTURE),
        ]
        for r in rooms:
            session.add(r)

        # Courses
        courses = [
            Course(code="CSCI401", title="Senior Capstone", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]),
        ]
        for c in courses:
            session.add(c)

        # Faculty
        faculty = [
            Faculty(
                id="F1", name="Prof A", email="a@test.com",
                max_credits=12, approved_courses=["CSCI401"],
            ),
        ]
        for f in faculty:
            session.add(f)

        # Senior student with graduation-critical demand
        students = [
            Student(id="SR1", name="Senior Student", email="sr1@test.com",
                   class_standing=ClassStanding.SR, credits_earned=100, major="CSCI",
                   catalog_year=2024),
        ]
        for s in students:
            session.add(s)

        # Graduation-critical demand
        demands = [
            StudentDemand(
                student_id="SR1",
                course_code="CSCI401",
                urgency_score=225.0,
                is_required=True,
                is_graduation_critical=True,
                reason="GRADUATION-CRITICAL; Major requirement; Senior priority"
            ),
        ]
        for d in demands:
            session.add(d)

        session.commit()

    return db_manager


def test_graduation_critical_satisfied(graduation_critical_dataset):
    """Test that graduation-critical demands are satisfied."""
    with graduation_critical_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Verify the senior got enrolled in CSCI401
        sr1_enrollments = [e for e in result.enrollments if e.student_id == "SR1"]
        enrolled_sections = [
            s for s in result.sections if any(e.section_id == s.id for e in sr1_enrollments)
        ]
        enrolled_courses = {s.course_code for s in enrolled_sections}

        assert "CSCI401" in enrolled_courses, "Graduation-critical demand not satisfied"


# ─────────────────────────────────────────────────────────────────
# Blocked Periods Tests
# ─────────────────────────────────────────────────────────────────


def test_compute_blocked_slot_ids_basic():
    """Test basic blocked period detection."""
    slots = [
        TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
        TimeSlot(id=2, name="MWF_1200", pattern="MWF", days=["M", "W", "F"],
                start_time="12:00", end_time="12:50", duration_minutes=50, slot_type="MWF_50"),
        TimeSlot(id=3, name="MWF_1300", pattern="MWF", days=["M", "W", "F"],
                start_time="13:00", end_time="13:50", duration_minutes=50, slot_type="MWF_50"),
        TimeSlot(id=4, name="TR_0800", pattern="TR", days=["T", "R"],
                start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
        TimeSlot(id=5, name="TR_1230", pattern="TR", days=["T", "R"],
                start_time="12:30", end_time="13:45", duration_minutes=75, slot_type="TR_75"),
    ]

    # Block 12:30-14:30 on MWF (common hour)
    blocked = [
        BlockedPeriod(
            name="Faculty Common Hour",
            days=["M", "W", "F"],
            start_time="12:30",
            end_time="14:30",
        )
    ]

    blocked_ids = compute_blocked_slot_ids(slots, blocked)

    # MWF 12:00-12:50 overlaps with 12:30-14:30 (overlap: 12:30-12:50)
    assert 2 in blocked_ids, "MWF 12:00 should be blocked"
    # MWF 13:00-13:50 is fully inside 12:30-14:30
    assert 3 in blocked_ids, "MWF 13:00 should be blocked"
    # MWF 08:00 is outside blocked period
    assert 1 not in blocked_ids, "MWF 08:00 should NOT be blocked"
    # TR slots should not be blocked (different days)
    assert 4 not in blocked_ids, "TR 08:00 should NOT be blocked"
    assert 5 not in blocked_ids, "TR 12:30 should NOT be blocked (different days)"


def test_compute_blocked_slot_ids_multiple_periods():
    """Test multiple blocked periods."""
    slots = [
        TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
        TimeSlot(id=2, name="TR_0800", pattern="TR", days=["T", "R"],
                start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
        TimeSlot(id=3, name="TR_1400", pattern="TR", days=["T", "R"],
                start_time="14:00", end_time="15:15", duration_minutes=75, slot_type="TR_75"),
    ]

    blocked = [
        BlockedPeriod(name="Morning meeting", days=["M"], start_time="07:00", end_time="09:00"),
        BlockedPeriod(name="Thursday event", days=["R"], start_time="14:00", end_time="16:00"),
    ]

    blocked_ids = compute_blocked_slot_ids(slots, blocked)

    # MWF 08:00 overlaps with Monday 7-9am
    assert 1 in blocked_ids
    # TR 08:00 does NOT overlap (T/R vs M)
    assert 2 not in blocked_ids
    # TR 14:00 overlaps with Thursday 2-4pm
    assert 3 in blocked_ids


def test_compute_blocked_slot_ids_no_periods():
    """Test with no blocked periods."""
    slots = [
        TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
    ]

    blocked_ids = compute_blocked_slot_ids(slots, [])
    assert len(blocked_ids) == 0


@pytest.fixture
def blocked_period_dataset(db_manager):
    """Create a dataset for testing blocked periods."""
    with db_manager.get_session() as session:
        # Time slots including some in the blocked period
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                    start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_1200", pattern="MWF", days=["M", "W", "F"],
                    start_time="12:00", end_time="12:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=3, name="MWF_1300", pattern="MWF", days=["M", "W", "F"],
                    start_time="13:00", end_time="13:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=4, name="MWF_1400", pattern="MWF", days=["M", "W", "F"],
                    start_time="14:00", end_time="14:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=5, name="TR_0800", pattern="TR", days=["T", "R"],
                    start_time="08:00", end_time="09:15", duration_minutes=75, slot_type="TR_75"),
        ]
        for s in slots:
            session.add(s)

        # Room
        session.add(
            Room(id="R1", building="Test", number="101", capacity=30, room_type=RoomType.LECTURE)
        )

        # Course
        session.add(Course(code="TEST101", title="Test Course", credits=3,
                          structure_type=StructureType.STANDALONE,
                          required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]))

        # Faculty
        session.add(
            Faculty(id="F1", name="Prof", email="p@test.com", max_credits=12, approved_courses=[])
        )

        # Student and demand
        session.add(Student(id="S1", name="Student", email="s@test.com",
                           class_standing=ClassStanding.FR, credits_earned=0, major="TEST",
                           catalog_year=2024))
        session.add(StudentDemand(student_id="S1", course_code="TEST101", urgency_score=10.0,
                                  is_required=True, is_graduation_critical=False, reason="Test"))

        session.commit()

    return db_manager


def test_blocked_periods_excluded_from_compatible_slots(blocked_period_dataset):
    """Test that blocked periods are excluded from compatible slots."""
    with blocked_period_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.blocked_periods = [
            BlockedPeriod(
                name="Faculty Common Hour",
                days=["M", "W", "F"],
                start_time="12:30",
                end_time="14:30",
            )
        ]

        engine = SchedulingEngine(session, conflict_matrix, config)

        # Check that blocked slots are excluded
        compatible = engine.compatible_slots.get("TEST101", [])
        compatible_ids = {s.id for s in compatible}

        # MWF 08:00 should be available
        assert 1 in compatible_ids, "MWF 08:00 should be available"
        # TR 08:00 should be available
        assert 5 in compatible_ids, "TR 08:00 should be available"
        # MWF 12:00, 13:00, 14:00 overlap with blocked period
        assert 2 not in compatible_ids, "MWF 12:00 should be blocked"
        assert 3 not in compatible_ids, "MWF 13:00 should be blocked"
        assert 4 not in compatible_ids, "MWF 14:00 should be blocked"


def test_solver_respects_blocked_periods(blocked_period_dataset):
    """Test that solver doesn't schedule in blocked periods."""
    with blocked_period_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.blocked_periods = [
            BlockedPeriod(
                name="Faculty Common Hour",
                days=["M", "W", "F"],
                start_time="12:30",
                end_time="14:30",
            )
        ]
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test Schedule", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Verify no meetings are scheduled in blocked slots
        blocked_slot_ids = {2, 3, 4}  # MWF 12:00, 13:00, 14:00
        for meeting in result.meetings:
            assert meeting.time_slot_id not in blocked_slot_ids, \
                f"Meeting scheduled in blocked slot {meeting.time_slot_id}"


# ─────────────────────────────────────────────────────────────────
# Pinning & Re-solve Tests
# ─────────────────────────────────────────────────────────────────


def test_pinned_section_stays_assigned(small_dataset):
    """Test that a pinned section keeps its assigned slot/room/faculty."""
    with small_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)

        # Create sections manually with one pinned
        sections = [
            Section(
                schedule_id=0,
                crn="90001",
                course_code="CSCI101",
                section_number="001",
                capacity=30,
                is_pinned=True,
                faculty_id="F1",
            ),
            Section(
                schedule_id=0,
                crn="90002",
                course_code="CSCI201",
                section_number="001",
                capacity=30,
                is_pinned=False,
            ),
        ]

        # Set pinned meeting data via engine's _pinned_meetings
        engine._pinned_meetings["90001"] = [(1, "R1")]  # MWF_0800, R1

        result = engine.solve(sections, "Pin Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Find the pinned section in results
        pinned = next((s for s in result.sections if s.crn == "90001"), None)
        assert pinned is not None
        assert pinned.faculty_id == "F1", "Pinned section should keep faculty F1"

        # Check meeting 0 has the pinned slot/room
        pinned_m0 = next(
            (m for m in result.meetings
             if m.section_id == pinned.id and m.meeting_sequence == 0),
            None,
        )
        assert pinned_m0 is not None
        assert pinned_m0.time_slot_id == 1, "Pinned section should stay in slot 1"
        assert pinned_m0.room_id == "R1", "Pinned section should stay in room R1"


def test_pinned_section_room_respected(small_dataset):
    """Test that other sections can't use a pinned section's room+slot."""
    with small_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)

        # Pin one section to slot 1, room R1
        sections = [
            Section(
                schedule_id=0,
                crn="90001",
                course_code="CSCI101",
                section_number="001",
                capacity=30,
                is_pinned=True,
                faculty_id="F1",
            ),
            Section(
                schedule_id=0,
                crn="90002",
                course_code="CSCI201",
                section_number="001",
                capacity=30,
                is_pinned=False,
            ),
        ]

        engine._pinned_meetings["90001"] = [(1, "R1")]  # MWF_0800, R1

        result = engine.solve(sections, "Room Respect Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        # The unpinned section should NOT be in slot 1 + room R1
        unpinned = next((s for s in result.sections if s.crn == "90002"), None)
        assert unpinned is not None
        unpinned_m0 = next(
            (m for m in result.meetings
             if m.section_id == unpinned.id and m.meeting_sequence == 0),
            None,
        )
        assert unpinned_m0 is not None
        assert not (unpinned_m0.time_slot_id == 1 and unpinned_m0.room_id == "R1"), \
            "Unpinned section should not use the same slot+room as pinned section"


def test_re_solve_preserves_pins(small_dataset):
    """Test that re_solve keeps pinned sections and re-assigns others."""
    with small_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)

        # First solve: generate a schedule
        sections = engine.create_sections("2025SP")
        result1 = engine.solve(sections, "Original", "2025SP")
        assert result1.status in ("optimal", "feasible")

        schedule_id = result1.sections[0].schedule_id

        # Pin the first section to a specific assignment
        first_section = result1.sections[0]
        first_section.is_pinned = True
        first_section.pin_reason = "Department request"
        session.add(first_section)
        session.commit()

        # Get the pinned meeting data from meeting 0
        first_m0 = session.exec(
            select(SectionMeeting).where(
                SectionMeeting.section_id == first_section.id,
                SectionMeeting.meeting_sequence == 0,
            )
        ).first()
        assert first_m0 is not None
        pinned_slot = first_m0.time_slot_id
        pinned_room = first_m0.room_id
        pinned_faculty = first_section.faculty_id

        # Re-solve
        result2 = engine.re_solve(schedule_id)
        assert result2.status in ("optimal", "feasible")

        # Find the pinned section in the new result
        pinned_in_new = next(
            (s for s in result2.sections if s.crn == first_section.crn), None
        )
        assert pinned_in_new is not None, "Pinned section should be in re-solved schedule"
        assert pinned_in_new.faculty_id == pinned_faculty

        # Check meeting 0 has the same slot/room
        new_m0 = next(
            (m for m in result2.meetings
             if m.section_id == pinned_in_new.id and m.meeting_sequence == 0),
            None,
        )
        assert new_m0 is not None
        assert new_m0.time_slot_id == pinned_slot
        assert new_m0.room_id == pinned_room

        # Verify the new schedule has a different ID
        new_schedule_id = result2.sections[0].schedule_id
        assert new_schedule_id != schedule_id, "Re-solve should create a new schedule"


@pytest.fixture
def tiny_room_dataset(db_manager):
    """One course, one 12-seat room, 20 students demanding it."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="TINY", building="Test", number="1", capacity=12,
                         room_type=RoomType.LECTURE))
        session.add(Course(code="CSCI101", title="Intro", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="Prof A", email="a@test.com", max_credits=12,
                            approved_courses=["CSCI101"]))
        for i in range(20):
            session.add(Student(id=f"S{i}", name=f"Student {i}", email=f"s{i}@test.com",
                                class_standing=ClassStanding.FR, credits_earned=0,
                                major="CSCI", catalog_year=2024))
            session.add(StudentDemand(student_id=f"S{i}", course_code="CSCI101",
                                      urgency_score=15.0, is_required=True,
                                      is_graduation_critical=False, reason="Test"))
        session.commit()
    return db_manager


def test_room_capacity_limits_enrollment(tiny_room_dataset):
    """Enrollment must be capped by the assigned room's capacity (12 < 20 demand)."""
    with tiny_room_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Capacity Test", "2025SP")
        assert result.status in ("optimal", "feasible")

        per_section: dict[int, int] = {}
        for e in result.enrollments:
            per_section[e.section_id] = per_section.get(e.section_id, 0) + 1
        assert per_section, "solver should enroll students"
        for section_id, count in per_section.items():
            assert count <= 12, (
                f"Section {section_id} enrolled {count} students into a 12-seat room"
            )


def test_room_capacity_flag_disables_enforcement(tiny_room_dataset):
    """respect_room_capacity=False restores the old section-capacity-only behavior."""
    with tiny_room_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.hard_constraints["respect_room_capacity"] = False
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Capacity Off Test", "2025SP")
        assert result.status in ("optimal", "feasible")

        total = len(result.enrollments)
        assert total == 20, f"with the flag off all 20 students fit (got {total})"


def test_infeasibility_diagnosis_names_conflicting_families(db_manager):
    """Two courses, ONE slot, ONE room: the sections physically collide.
    The solver must report WHICH families conflict (assignment +
    room-no-overlap) instead of a bare INFEASIBLE."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="TINY", building="T", number="1", capacity=12,
                         room_type=RoomType.LECTURE))
        for code in ("BIO101", "CHM101"):
            session.add(Course(code=code, title=code, credits=3,
                               structure_type=StructureType.STANDALONE,
                               required_room_types=["LECTURE"],
                               valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="P", email="p@t.co", max_credits=12))
        for i in range(20):
            session.add(Student(id=f"S{i}", name=f"s{i}", email="", major="BIOL",
                                class_standing=ClassStanding.SR, credits_earned=100,
                                catalog_year=2023))
            # every student critically needs BOTH courses -> 40 critical seats
            for code in ("BIO101", "CHM101"):
                session.add(StudentDemand(student_id=f"S{i}", course_code=code,
                                          urgency_score=90.0, is_required=True,
                                          is_graduation_critical=True, reason="t"))
        session.commit()

        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()
        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30
        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2026FA")
        result = engine.solve(sections, "Infeasible Test", "2026FA")

        assert result.status == "infeasible"
        assert result.infeasible_core, "diagnosis must name the conflicting families"
        # The MINIMAL clash here: two sections, one slot, one room — they
        # collide physically before capacity ever matters. The core must say
        # exactly that (and nothing about unrelated families).
        assert "room-no-overlap" in result.infeasible_core
        assert "assignment" in result.infeasible_core
        assert "travel-time" not in result.infeasible_core


def test_diagnosis_neutralizes_stale_open_vars(db_manager):
    """Regression: _diagnose_infeasibility rebuilds the model with a FRESH
    CpModel, so it must neutralize self._open_vars (closure BoolVars belong to
    the prior, now-dead model) during the rebuild and restore the attribute
    after. Without the guard the constraint builders consume the stale value
    (e.g. _add_capacity_constraints indexes open_vars[crn]) and diagnosis
    silently fails, returning None instead of naming the conflict.
    """
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="TINY", building="T", number="1", capacity=12,
                         room_type=RoomType.LECTURE))
        for code in ("BIO101", "CHM101"):
            session.add(Course(code=code, title=code, credits=3,
                               structure_type=StructureType.STANDALONE,
                               required_room_types=["LECTURE"],
                               valid_slot_types=["MWF_50"]))
        session.add(Faculty(id="F1", name="P", email="p@t.co", max_credits=12))
        for i in range(20):
            session.add(Student(id=f"S{i}", name=f"s{i}", email="", major="BIOL",
                                class_standing=ClassStanding.SR, credits_earned=100,
                                catalog_year=2023))
            for code in ("BIO101", "CHM101"):
                session.add(StudentDemand(student_id=f"S{i}", course_code=code,
                                          urgency_score=90.0, is_required=True,
                                          is_graduation_critical=True, reason="t"))
        session.commit()

        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()
        engine = SchedulingEngine(session, conflict_matrix, SchedulerConfig())
        sections = engine.create_sections("2026FA")

        # Stand in for closure BoolVars left on the engine by a prior solve.
        # A non-None, non-dict sentinel makes the failure deterministic: the
        # unguarded builders would raise on it (caught → None) instead of
        # silently mis-indexing into the fresh model.
        sentinel = object()
        engine._open_vars = sentinel

        core = engine._diagnose_infeasibility(sections, time_limit=15)

        assert core, "diagnosis must name families despite a stale _open_vars"
        assert "room-no-overlap" in core
        assert engine._open_vars is sentinel, "diagnosis must restore _open_vars"


def test_optimality_gap_reported(tiny_room_dataset):
    with tiny_room_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()
        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30
        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2026FA")
        result = engine.solve(sections, "Gap Test", "2026FA")

        assert result.status in ("optimal", "feasible")
        assert "optimality_gap_pct" in result.stats
        assert result.stats["optimality_gap_pct"] >= 0.0
