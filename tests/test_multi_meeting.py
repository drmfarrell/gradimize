"""Tests for multi-meeting section support."""

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
def single_meeting_dataset(db_manager):
    """Standard single-meeting courses (no meeting_patterns)."""
    with db_manager.get_session() as session:
        slots = [
            TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
        ]
        for s in slots:
            session.add(s)

        session.add(
            Room(id="R1", building="Main", number="101", capacity=30, room_type=RoomType.LECTURE)
        )

        session.add(Course(
            code="CSCI101", title="Intro CS", credits=3,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
            meeting_patterns=[],
        ))

        session.add(Faculty(id="F1", name="Prof A", email="a@test.com",
                            max_credits=12, approved_courses=["CSCI101"]))

        session.add(Student(id="S1", name="Student 1", email="s1@test.com",
                            class_standing=ClassStanding.FR, credits_earned=0,
                            major="CSCI", catalog_year=2024))
        session.add(StudentDemand(student_id="S1", course_code="CSCI101",
                                  urgency_score=10.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
        session.commit()

    return db_manager


@pytest.fixture
def multi_meeting_dataset(db_manager):
    """Dataset with a 2-meeting course (lecture T + lab R)."""
    with db_manager.get_session() as session:
        # Slots: T lecture, R lab, plus standard MWF for a normal course
        slots = [
            TimeSlot(id=1, name="T_1500", pattern="T", days=["T"],
                     start_time="15:00", end_time="17:00", duration_minutes=120, slot_type="T_120"),
            TimeSlot(id=2, name="R_1200", pattern="R", days=["R"],
                     start_time="12:00", end_time="18:00", duration_minutes=360, slot_type="R_360"),
            TimeSlot(id=3, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=4, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
        ]
        for s in slots:
            session.add(s)

        # Rooms
        rooms = [
            Room(id="LEC1", building="Main", number="101", capacity=30, room_type=RoomType.LECTURE),
            Room(
                id="LAB1", building="Science", number="201", capacity=20,
                room_type=RoomType.LAB_BIO,
            ),
        ]
        for r in rooms:
            session.add(r)

        # Multi-meeting course: BMED3301 - lecture on T, lab on R
        session.add(Course(
            code="BMED3301", title="Biomedical Research Methods", credits=4,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"],  # Course-level (backward compat)
            valid_slot_types=["T_120"],  # Course-level (backward compat)
            meeting_patterns=[
                {"slot_types": ["T_120"], "room_types": ["LECTURE"]},
                {"slot_types": ["R_360"], "room_types": ["LAB_BIO"]},
            ],
        ))

        # Normal single-meeting course
        session.add(Course(
            code="CSCI101", title="Intro CS", credits=3,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
            meeting_patterns=[],
        ))

        # Faculty
        session.add(Faculty(id="F1", name="Prof A", email="a@test.com",
                            max_credits=12, approved_courses=[]))

        # Students + demand
        session.add(Student(id="S1", name="Student 1", email="s1@test.com",
                            class_standing=ClassStanding.JR, credits_earned=60,
                            major="BMED", catalog_year=2024))
        session.add(StudentDemand(student_id="S1", course_code="BMED3301",
                                  urgency_score=20.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
        session.add(StudentDemand(student_id="S1", course_code="CSCI101",
                                  urgency_score=10.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
        session.commit()

    return db_manager


def test_single_meeting_creates_one_section_meeting(single_meeting_dataset):
    """Standard course creates exactly 1 SectionMeeting per section."""
    with single_meeting_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test", "2025SP")

        assert result.status in ("optimal", "feasible")
        assert len(result.sections) > 0

        # Each section should have exactly 1 SectionMeeting
        for section in result.sections:
            meetings = list(
                session.exec(
                    select(SectionMeeting).where(SectionMeeting.section_id == section.id)
                ).all()
            )
            assert len(meetings) == 1, (
                f"Section {section.crn} should have 1 meeting, got {len(meetings)}"
            )
            assert meetings[0].meeting_sequence == 0


def test_multi_meeting_creates_multiple_section_meetings(multi_meeting_dataset):
    """Multi-meeting course creates 2 SectionMeeting records per section."""
    with multi_meeting_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Find BMED3301 sections
        bmed_sections = [s for s in result.sections if s.course_code == "BMED3301"]
        assert len(bmed_sections) > 0

        for section in bmed_sections:
            meetings = list(
                session.exec(
                    select(SectionMeeting)
                    .where(SectionMeeting.section_id == section.id)
                    .order_by(SectionMeeting.meeting_sequence)  # type: ignore
                ).all()
            )
            assert len(meetings) == 2, (
                f"BMED3301 section should have 2 meetings, got {len(meetings)}"
            )
            assert meetings[0].meeting_sequence == 0
            assert meetings[1].meeting_sequence == 1


def test_multi_meeting_different_rooms(multi_meeting_dataset):
    """Each meeting of a multi-meeting course can be in a different room type."""
    with multi_meeting_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        bmed_sections = [s for s in result.sections if s.course_code == "BMED3301"]
        for section in bmed_sections:
            meetings = list(
                session.exec(
                    select(SectionMeeting)
                    .where(SectionMeeting.section_id == section.id)
                    .order_by(SectionMeeting.meeting_sequence)  # type: ignore
                ).all()
            )
            assert len(meetings) == 2

            # Meeting 0 should be in a LECTURE room
            room0 = session.get(Room, meetings[0].room_id)
            assert room0.room_type == RoomType.LECTURE, (
                f"Meeting 0 should be LECTURE, got {room0.room_type}"
            )

            # Meeting 1 should be in a LAB_BIO room
            room1 = session.get(Room, meetings[1].room_id)
            assert room1.room_type == RoomType.LAB_BIO, (
                f"Meeting 1 should be LAB_BIO, got {room1.room_type}"
            )


def test_multi_meeting_no_self_overlap(multi_meeting_dataset):
    """Meetings of the same section don't overlap."""
    with multi_meeting_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        bmed_sections = [s for s in result.sections if s.course_code == "BMED3301"]
        for section in bmed_sections:
            meetings = list(
                session.exec(
                    select(SectionMeeting)
                    .where(SectionMeeting.section_id == section.id)
                ).all()
            )

            # Check no overlap between meetings (different days in this test)
            for m1, m2 in zip(meetings, meetings[1:]):
                slot1 = session.get(TimeSlot, m1.time_slot_id)
                slot2 = session.get(TimeSlot, m2.time_slot_id)
                days1 = set(slot1.days)
                days2 = set(slot2.days)
                if days1 & days2:
                    # Same day: times must not overlap
                    from src.core.timeutil import times_overlap
                    assert not times_overlap(
                        slot1.start_time, slot1.end_time,
                        slot2.start_time, slot2.end_time
                    ), "Meetings of same section must not overlap"


def test_multi_meeting_faculty_no_overlap(db_manager):
    """Faculty can't have overlapping meetings across different sections.

    Two courses taught by the same faculty: one multi-meeting (T lecture + R lab)
    and one single-meeting (MWF). They should both get scheduled without overlap
    because their meetings are on different days.
    """
    with db_manager.get_session() as session:
        slots = [
            TimeSlot(id=1, name="T_0800", pattern="T", days=["T"],
                     start_time="08:00", end_time="10:00", duration_minutes=120, slot_type="T_120"),
            TimeSlot(id=2, name="R_1200", pattern="R", days=["R"],
                     start_time="12:00", end_time="18:00", duration_minutes=360, slot_type="R_360"),
            TimeSlot(id=3, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                     start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
            TimeSlot(id=4, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                     start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
        ]
        for s in slots:
            session.add(s)

        session.add(
            Room(
                id="LEC1", building="Main", number="101", capacity=30,
                room_type=RoomType.LECTURE,
            )
        )
        session.add(
            Room(
                id="LAB1", building="Sci", number="201", capacity=20, room_type=RoomType.LAB_BIO
            )
        )

        # Multi-meeting course: T lecture + R lab
        session.add(Course(
            code="BMED3301", title="Biomed Methods", credits=4,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["T_120"],
            meeting_patterns=[
                {"slot_types": ["T_120"], "room_types": ["LECTURE"]},
                {"slot_types": ["R_360"], "room_types": ["LAB_BIO"]},
            ],
        ))
        # Single-meeting MWF course (no day overlap with BMED3301)
        session.add(Course(
            code="BMED3302", title="Biomed Seminar", credits=3,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
            meeting_patterns=[],
        ))

        # One faculty teaches both
        session.add(Faculty(id="F1", name="Prof A", email="a@test.com",
                            max_credits=12, approved_courses=["BMED3301", "BMED3302"]))

        session.add(Student(id="S1", name="Student", email="s@test.com",
                            class_standing=ClassStanding.JR, credits_earned=60,
                            major="BMED", catalog_year=2024))
        session.add(StudentDemand(student_id="S1", course_code="BMED3301",
                                  urgency_score=20.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
        session.add(StudentDemand(student_id="S1", course_code="BMED3302",
                                  urgency_score=15.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
        session.commit()

        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP", {"BMED3301": 1, "BMED3302": 1})
        result = engine.solve(sections, "Faculty Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        # Both courses should be scheduled and taught by F1
        for section in result.sections:
            assert section.faculty_id == "F1"

        # Verify BMED3301 has 2 meetings and BMED3302 has 1
        for section in result.sections:
            meetings = list(
                session.exec(
                    select(SectionMeeting).where(SectionMeeting.section_id == section.id)
                ).all()
            )
            expected = 2 if section.course_code == "BMED3301" else 1
            assert len(meetings) == expected


def test_meeting_patterns_csv_ingestion(db_manager):
    """Meeting patterns CSV loads correctly into Course.meeting_patterns."""
    import csv

    with db_manager.get_session() as session:
        # Create a course first
        session.add(Course(
            code="BMED3301", title="Biomed Methods", credits=4,
            structure_type=StructureType.STANDALONE,
            required_room_types=["LECTURE"], valid_slot_types=["T_120"],
        ))
        session.commit()

    # Write meeting patterns CSV to temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = Path(tmpdir) / "meeting_patterns.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["course_code", "meeting_seq", "valid_slot_types", "required_room_types"]
            )
            writer.writerow(["BMED3301", "0", "T_120", "LECTURE"])
            writer.writerow(["BMED3301", "1", "R_360", "LAB_BIO"])

        with db_manager.get_session() as session:
            from src.core.ingest import DataIngestor
            ingestor = DataIngestor(session, tmpdir)
            updated = ingestor.load_meeting_patterns()
            assert updated == 1

            course = session.get(Course, "BMED3301")
            assert len(course.meeting_patterns) == 2
            assert course.meeting_patterns[0]["slot_types"] == ["T_120"]
            assert course.meeting_patterns[0]["room_types"] == ["LECTURE"]
            assert course.meeting_patterns[1]["slot_types"] == ["R_360"]
            assert course.meeting_patterns[1]["room_types"] == ["LAB_BIO"]


def test_banner_export_multi_meeting(multi_meeting_dataset):
    """Export produces multiple rows for multi-meeting sections."""
    with multi_meeting_dataset.get_session() as session:
        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()

        config = SchedulerConfig()
        config.solver_settings["max_time_seconds"] = 30

        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Export Test", "2025SP")

        assert result.status in ("optimal", "feasible")

        from src.core.export import BannerExporter
        exporter = BannerExporter(session)
        schedule_id = result.sections[0].schedule_id
        csv_content = exporter.export_schedule(schedule_id)

        # Parse CSV
        import csv
        from io import StringIO
        reader = csv.DictReader(StringIO(csv_content))
        rows = list(reader)

        # Count rows per CRN
        crn_counts: dict[str, int] = {}
        for row in rows:
            crn = row["CRN"]
            crn_counts[crn] = crn_counts.get(crn, 0) + 1

        # BMED3301 sections should have 2 rows each (2 meetings)
        bmed_sections = [s for s in result.sections if s.course_code == "BMED3301"]
        for section in bmed_sections:
            assert crn_counts.get(section.crn, 0) == 2, (
                f"BMED3301 CRN {section.crn} should have 2 CSV rows, got "
                f"{crn_counts.get(section.crn, 0)}"
            )

        # CSCI101 sections should have 1 row each (1 meeting)
        csci_sections = [s for s in result.sections if s.course_code == "CSCI101"]
        for section in csci_sections:
            assert crn_counts.get(section.crn, 0) == 1, (
                f"CSCI101 CRN {section.crn} should have 1 CSV row, got "
                f"{crn_counts.get(section.crn, 0)}"
            )
