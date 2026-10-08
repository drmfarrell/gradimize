"""Tests for schedule import and recommendation engine."""

import csv
import tempfile
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.export import BannerExporter
from src.core.ingest import DataIngestor
from src.core.recommend import (
    RecommendationType,
    analyze_schedule,
)
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Faculty,
    Room,
    RoomType,
    ScheduleVersion,
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
def import_ready_db(db_manager):
    """Database with TimeSlots, Rooms, Faculty, Courses, Students, Demands."""
    with db_manager.get_session() as session:
        # Time slots
        slots = [
            TimeSlot(
                id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                start_time="08:00", end_time="08:50", duration_minutes=50,
                slot_type="MWF_50",
            ),
            TimeSlot(
                id=2, name="TR_0930", pattern="TR", days=["T", "R"],
                start_time="09:30", end_time="10:45", duration_minutes=75,
                slot_type="TR_75",
            ),
            TimeSlot(
                id=3, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                start_time="09:00", end_time="09:50", duration_minutes=50,
                slot_type="MWF_50",
            ),
            TimeSlot(
                id=4, name="T_1500", pattern="T", days=["T"],
                start_time="15:00", end_time="17:00", duration_minutes=120,
                slot_type="T_120",
            ),
            TimeSlot(
                id=5, name="R_1200", pattern="R", days=["R"],
                start_time="12:00", end_time="18:00", duration_minutes=360,
                slot_type="R_360",
            ),
        ]
        for s in slots:
            session.add(s)

        # Rooms
        rooms = [
            Room(id="SCI-101", building="Science", number="101", capacity=30,
                 room_type=RoomType.LECTURE),
            Room(id="SCI-201", building="Science", number="201", capacity=40,
                 room_type=RoomType.LECTURE),
            Room(id="SCI-301", building="Science", number="301", capacity=20,
                 room_type=RoomType.LAB_BIO),
            Room(id="TINY-100", building="Tiny", number="100", capacity=10,
                 room_type=RoomType.LECTURE),
        ]
        for r in rooms:
            session.add(r)

        # Faculty
        faculty = [
            Faculty(id="FSMITH01", name="Dr. Smith", email="smith@test.edu",
                    max_credits=12, approved_courses=["CSCI1301", "CSCI2301"]),
            Faculty(id="FJONES01", name="Dr. Jones", email="jones@test.edu",
                    max_credits=6, approved_courses=["BIOL1401"]),
        ]
        for f in faculty:
            session.add(f)

        # Courses
        courses = [
            Course(code="CSCI1301", title="Intro to CS", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]),
            Course(code="CSCI2301", title="Data Structures", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]),
            Course(code="BIOL1401", title="Biology I", credits=4,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["TR_75"]),
            Course(code="MATH1301", title="Calculus I", credits=3,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["MWF_50", "TR_75"]),
            Course(code="BMED3301", title="Biomed Methods", credits=4,
                   structure_type=StructureType.STANDALONE,
                   required_room_types=["LECTURE"], valid_slot_types=["T_120"],
                   meeting_patterns=[
                       {"slot_types": ["T_120"], "room_types": ["LECTURE"]},
                       {"slot_types": ["R_360"], "room_types": ["LAB_BIO"]},
                   ]),
        ]
        for c in courses:
            session.add(c)

        # Students and demands
        for i in range(1, 11):
            session.add(Student(
                id=f"S{i:03d}", name=f"Student {i}", email=f"s{i}@test.edu",
                class_standing=ClassStanding.SO, credits_earned=30,
                major="CSCI", catalog_year=2024,
            ))
            session.add(StudentDemand(
                student_id=f"S{i:03d}", course_code="CSCI1301",
                urgency_score=10.0, is_required=True,
                is_graduation_critical=False, reason="Major req",
            ))
            if i <= 5:
                session.add(StudentDemand(
                    student_id=f"S{i:03d}", course_code="CSCI2301",
                    urgency_score=8.0, is_required=True,
                    is_graduation_critical=False, reason="Major req",
                ))
            if i <= 3:
                session.add(StudentDemand(
                    student_id=f"S{i:03d}", course_code="BIOL1401",
                    urgency_score=5.0, is_required=False,
                    is_graduation_critical=False, reason="Elective",
                ))
            if i <= 2:
                session.add(StudentDemand(
                    student_id=f"S{i:03d}", course_code="MATH1301",
                    urgency_score=12.0, is_required=True,
                    is_graduation_critical=False, reason="Major req",
                ))

        # Degree requirements (needed for some tests)
        session.add(DegreeRequirement(
            major="CSCI", course_code="CSCI1301", is_required=True,
            category="core", catalog_year=2024,
        ))
        session.add(DegreeRequirement(
            major="CSCI", course_code="CSCI2301", is_required=True,
            category="core", catalog_year=2024,
        ))

        session.commit()

    return db_manager


def _make_csv(*rows):
    """Helper to build a Banner CSV string from row dicts."""
    header = [
        "CRN", "SUBJECT", "COURSE_NUMBER", "SECTION", "TITLE", "CREDITS",
        "CAMPUS", "STATUS", "MEETING_DAYS", "START_TIME", "END_TIME",
        "BUILDING", "ROOM", "CAPACITY", "ENROLLMENT", "INSTRUCTOR_ID",
        "INSTRUCTOR_NAME", "PART_OF_TERM",
    ]
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(h, "") for h in header])
    return output.getvalue()


# ─────────────────────────────────────────────────────────────────
# Import Tests
# ─────────────────────────────────────────────────────────────────


def test_import_single_meeting_section(import_ready_db):
    """CSV with one row per section creates Section + SectionMeeting, is_pinned=True."""
    csv_content = _make_csv({
        "CRN": "10001", "SUBJECT": "CSCI", "COURSE_NUMBER": "1301",
        "SECTION": "001", "TITLE": "Intro to CS", "CREDITS": "3",
        "CAMPUS": "MAIN", "STATUS": "A", "MEETING_DAYS": "MWF",
        "START_TIME": "08:00", "END_TIME": "08:50",
        "BUILDING": "Science", "ROOM": "101", "CAPACITY": "30",
        "ENROLLMENT": "25", "INSTRUCTOR_ID": "FSMITH01",
        "INSTRUCTOR_NAME": "Dr. Smith", "PART_OF_TERM": "1",
    })

    with import_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.schedule_id is not None
        assert result.sections_imported == 1
        assert result.meetings_imported == 1
        assert len(result.errors) == 0

        section = session.exec(
            select(Section).where(Section.crn == "10001")
        ).first()
        assert section is not None
        assert section.is_pinned is True
        assert section.pin_reason == "Imported"
        assert section.faculty_id == "FSMITH01"
        assert section.course_code == "CSCI1301"

        meetings = list(session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id == section.id)
        ).all())
        assert len(meetings) == 1
        assert meetings[0].meeting_sequence == 0


def test_import_multi_meeting_section(import_ready_db):
    """Same CRN in two rows creates two SectionMeeting records with seq 0, 1."""
    csv_content = _make_csv(
        {
            "CRN": "20001", "SUBJECT": "BMED", "COURSE_NUMBER": "3301",
            "SECTION": "001", "TITLE": "Biomed Methods", "CREDITS": "4",
            "CAMPUS": "MAIN", "STATUS": "A", "MEETING_DAYS": "T",
            "START_TIME": "15:00", "END_TIME": "17:00",
            "BUILDING": "Science", "ROOM": "101", "CAPACITY": "20",
            "ENROLLMENT": "15", "INSTRUCTOR_ID": "FJONES01",
            "INSTRUCTOR_NAME": "Dr. Jones", "PART_OF_TERM": "1",
        },
        {
            "CRN": "20001", "SUBJECT": "BMED", "COURSE_NUMBER": "3301",
            "SECTION": "001", "TITLE": "Biomed Methods", "CREDITS": "4",
            "CAMPUS": "MAIN", "STATUS": "A", "MEETING_DAYS": "R",
            "START_TIME": "12:00", "END_TIME": "18:00",
            "BUILDING": "Science", "ROOM": "301", "CAPACITY": "20",
            "ENROLLMENT": "15", "INSTRUCTOR_ID": "FJONES01",
            "INSTRUCTOR_NAME": "Dr. Jones", "PART_OF_TERM": "1",
        },
    )

    with import_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.sections_imported == 1
        assert result.meetings_imported == 2
        assert len(result.errors) == 0

        section = session.exec(
            select(Section).where(Section.crn == "20001")
        ).first()
        assert section is not None

        meetings = list(session.exec(
            select(SectionMeeting)
            .where(SectionMeeting.section_id == section.id)
            .order_by(SectionMeeting.meeting_sequence)  # type: ignore
        ).all())
        assert len(meetings) == 2
        assert meetings[0].meeting_sequence == 0
        assert meetings[1].meeting_sequence == 1


def test_import_unmatched_timeslot(import_ready_db):
    """Unknown time generates a warning."""
    csv_content = _make_csv({
        "CRN": "30001", "SUBJECT": "CSCI", "COURSE_NUMBER": "1301",
        "SECTION": "001", "TITLE": "Intro to CS", "CREDITS": "3",
        "CAMPUS": "MAIN", "STATUS": "A", "MEETING_DAYS": "MWF",
        "START_TIME": "07:00", "END_TIME": "07:50",
        "BUILDING": "Science", "ROOM": "101", "CAPACITY": "30",
        "ENROLLMENT": "0", "INSTRUCTOR_ID": "FSMITH01",
        "INSTRUCTOR_NAME": "Dr. Smith", "PART_OF_TERM": "1",
    })

    with import_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.sections_imported == 1
        assert any("no matching time slot" in w for w in result.warnings)


def test_import_room_matched_by_building_code(import_ready_db):
    """BUILDING holding the Banner building CODE still links the room: the
    code + number falls back to the room-id convention (SCI + 101 -> SCI-101)
    even though Room.building says "Science"."""
    csv_content = _make_csv({
        "CRN": "10005", "SUBJECT": "CSCI", "COURSE_NUMBER": "1301",
        "SECTION": "001", "MEETING_DAYS": "MWF",
        "START_TIME": "08:00", "END_TIME": "08:50",
        "BUILDING": "SCI", "ROOM": "101", "CAPACITY": "30",
        "INSTRUCTOR_ID": "FSMITH01",
    })

    with import_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.sections_imported == 1
        assert result.meetings_imported == 1
        assert not any("no matching room" in w for w in result.warnings)
        section = session.exec(
            select(Section).where(Section.crn == "10005")
        ).first()
        meeting = session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id == section.id)
        ).first()
        assert meeting.room_id == "SCI-101"


def test_import_unmatched_course(import_ready_db):
    """Unknown SUBJECT+NUMBER generates an error and section is skipped."""
    csv_content = _make_csv({
        "CRN": "40001", "SUBJECT": "PHYS", "COURSE_NUMBER": "1401",
        "SECTION": "001", "TITLE": "Physics I", "CREDITS": "4",
        "CAMPUS": "MAIN", "STATUS": "A", "MEETING_DAYS": "MWF",
        "START_TIME": "08:00", "END_TIME": "08:50",
        "BUILDING": "Science", "ROOM": "101", "CAPACITY": "30",
        "ENROLLMENT": "0", "INSTRUCTOR_ID": "", "INSTRUCTOR_NAME": "TBA",
        "PART_OF_TERM": "1",
    })

    with import_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.sections_imported == 0
        assert any("unknown course PHYS1401" in e for e in result.errors)


def test_import_unmatched_faculty(import_ready_db):
    """Unknown INSTRUCTOR_ID generates a warning, section imported with faculty_id=None."""
    csv_content = _make_csv({
        "CRN": "50001", "SUBJECT": "CSCI", "COURSE_NUMBER": "1301",
        "SECTION": "001", "TITLE": "Intro to CS", "CREDITS": "3",
        "CAMPUS": "MAIN", "STATUS": "A", "MEETING_DAYS": "MWF",
        "START_TIME": "08:00", "END_TIME": "08:50",
        "BUILDING": "Science", "ROOM": "101", "CAPACITY": "30",
        "ENROLLMENT": "0", "INSTRUCTOR_ID": "FUNKNOWN",
        "INSTRUCTOR_NAME": "Unknown", "PART_OF_TERM": "1",
    })

    with import_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(csv_content=csv_content)

        assert result.sections_imported == 1
        assert any("unknown faculty FUNKNOWN" in w for w in result.warnings)

        section = session.exec(
            select(Section).where(Section.crn == "50001")
        ).first()
        assert section.faculty_id is None


def test_import_creates_schedule_version(import_ready_db):
    """ScheduleVersion created with status='imported'."""
    csv_content = _make_csv({
        "CRN": "60001", "SUBJECT": "CSCI", "COURSE_NUMBER": "1301",
        "SECTION": "001", "TITLE": "Intro to CS", "CREDITS": "3",
        "CAMPUS": "MAIN", "STATUS": "A", "MEETING_DAYS": "MWF",
        "START_TIME": "08:00", "END_TIME": "08:50",
        "BUILDING": "Science", "ROOM": "101", "CAPACITY": "30",
        "ENROLLMENT": "0", "INSTRUCTOR_ID": "FSMITH01",
        "INSTRUCTOR_NAME": "Dr. Smith", "PART_OF_TERM": "1",
    })

    with import_ready_db.get_session() as session:
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(
            csv_content=csv_content,
            schedule_name="Test Import",
            semester="2025FA",
        )

        schedule = session.get(ScheduleVersion, result.schedule_id)
        assert schedule is not None
        assert schedule.status == "imported"
        assert schedule.name == "Test Import"
        assert schedule.semester == "2025FA"


def test_import_roundtrip(import_ready_db):
    """Export a generated schedule then import it back — same section count."""
    with import_ready_db.get_session() as session:
        # Manually create a schedule with sections to export
        schedule = ScheduleVersion(
            name="Original", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        section = Section(
            schedule_id=schedule.id, crn="RT001",
            course_code="CSCI1301", section_number="001",
            capacity=30, faculty_id="FSMITH01",
        )
        session.add(section)
        session.commit()
        session.refresh(section)

        sm = SectionMeeting(
            section_id=section.id, meeting_sequence=0,
            time_slot_id=1, room_id="SCI-101",
        )
        session.add(sm)
        session.commit()

        # Export
        exporter = BannerExporter(session)
        csv_content = exporter.export_schedule(schedule.id)

        # Import back
        ingestor = DataIngestor(session, ".")
        result = ingestor.import_banner_schedule(
            csv_content=csv_content, schedule_name="Roundtrip",
        )

        assert result.sections_imported == 1
        assert result.meetings_imported == 1
        assert len(result.errors) == 0


# ─────────────────────────────────────────────────────────────────
# Recommendation Tests
# ─────────────────────────────────────────────────────────────────


def test_recommend_unscheduled_course(import_ready_db):
    """Demand exists but no section → UNSCHEDULED_COURSE."""
    with import_ready_db.get_session() as session:
        # Create an empty schedule (no sections)
        schedule = ScheduleVersion(
            name="Empty", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        recs = analyze_schedule(session, schedule.id)
        unscheduled = [r for r in recs if r.type == RecommendationType.UNSCHEDULED_COURSE]
        # All demanded courses should appear
        codes = {r.course_code for r in unscheduled}
        assert "CSCI1301" in codes
        assert "CSCI2301" in codes


def test_recommend_insufficient_sections(import_ready_db):
    """50 demands, 1 section cap 30 → INSUFFICIENT_SECTIONS."""
    with import_ready_db.get_session() as session:
        # Add extra students demanding CSCI1301 (already have 10, add 40 more)
        for i in range(11, 51):
            session.add(Student(
                id=f"X{i:03d}", name=f"Extra {i}", email=f"x{i}@test.edu",
                class_standing=ClassStanding.FR, credits_earned=0,
                major="CSCI", catalog_year=2024,
            ))
            session.add(StudentDemand(
                student_id=f"X{i:03d}", course_code="CSCI1301",
                urgency_score=10.0, is_required=True,
                is_graduation_critical=False, reason="Major req",
            ))
        session.commit()

        # Create schedule with 1 section of cap 30
        schedule = ScheduleVersion(
            name="Test", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        section = Section(
            schedule_id=schedule.id, crn="IS001",
            course_code="CSCI1301", section_number="001",
            capacity=30, faculty_id="FSMITH01",
        )
        session.add(section)
        session.commit()
        session.refresh(section)

        session.add(SectionMeeting(
            section_id=section.id, meeting_sequence=0,
            time_slot_id=1, room_id="SCI-101",
        ))
        session.commit()

        recs = analyze_schedule(session, schedule.id)
        insufficient = [
            r for r in recs
            if r.type == RecommendationType.INSUFFICIENT_SECTIONS
            and r.course_code == "CSCI1301"
        ]
        assert len(insufficient) == 1
        assert insufficient[0].priority > 0


def test_recommend_low_demand(import_ready_db):
    """2 demands, section cap 30 → LOW_DEMAND."""
    with import_ready_db.get_session() as session:
        # MATH1301 has only 2 demands, capacity 30 → < 30%
        schedule = ScheduleVersion(
            name="Test", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        section = Section(
            schedule_id=schedule.id, crn="LD001",
            course_code="MATH1301", section_number="001",
            capacity=30,
        )
        session.add(section)
        session.commit()
        session.refresh(section)

        session.add(SectionMeeting(
            section_id=section.id, meeting_sequence=0,
            time_slot_id=1, room_id="SCI-101",
        ))
        session.commit()

        recs = analyze_schedule(session, schedule.id)
        low = [
            r for r in recs
            if r.type == RecommendationType.LOW_DEMAND
            and r.course_code == "MATH1301"
        ]
        assert len(low) == 1


def test_recommend_room_capacity(import_ready_db):
    """Expected enrollment > room cap → ROOM_CAPACITY."""
    with import_ready_db.get_session() as session:
        # CSCI1301 has 10 demands; put it in a room with capacity 5 (TINY-100 cap=10 won't trigger)
        # Use TINY-100 (cap 10) for a course with more demand per section
        # Actually: 10 demands / 1 section = 10 expected, room cap = 10 → not > cap
        # Let's use a single section in TINY-100 for CSCI1301 (10 demands, cap 10) — borderline
        # Better: put BIOL1401 (3 demands) in TINY-100 → 3/1 = 3, cap 10 → no trigger
        # Let's add more demands to trigger: we need expected > room.capacity
        # Add 20 more demands for CSCI1301 → 30 demands / 1 section = 30 expected, room cap 10
        # → triggers
        for i in range(50, 70):
            session.add(Student(
                id=f"RC{i:03d}", name=f"RC {i}", email=f"rc{i}@test.edu",
                class_standing=ClassStanding.FR, credits_earned=0,
                major="CSCI", catalog_year=2024,
            ))
            session.add(StudentDemand(
                student_id=f"RC{i:03d}", course_code="CSCI1301",
                urgency_score=10.0, is_required=True,
                is_graduation_critical=False, reason="Major req",
            ))
        session.commit()

        schedule = ScheduleVersion(
            name="Test", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        section = Section(
            schedule_id=schedule.id, crn="RC001",
            course_code="CSCI1301", section_number="001",
            capacity=10,
        )
        session.add(section)
        session.commit()
        session.refresh(section)

        session.add(SectionMeeting(
            section_id=section.id, meeting_sequence=0,
            time_slot_id=1, room_id="TINY-100",
        ))
        session.commit()

        recs = analyze_schedule(session, schedule.id)
        room_cap = [
            r for r in recs
            if r.type == RecommendationType.ROOM_CAPACITY
            and r.course_code == "CSCI1301"
        ]
        assert len(room_cap) >= 1


def test_recommend_faculty_overload(import_ready_db):
    """Faculty over max_credits → FACULTY_OVERLOAD."""
    with import_ready_db.get_session() as session:
        # FJONES01 has max_credits=6. Assign them 3 sections of BIOL1401 (4 credits each)
        # = 12 credits
        schedule = ScheduleVersion(
            name="Test", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        for i in range(3):
            sec = Section(
                schedule_id=schedule.id, crn=f"FO00{i}",
                course_code="BIOL1401", section_number=f"00{i+1}",
                capacity=30, faculty_id="FJONES01",
            )
            session.add(sec)
            session.commit()
            session.refresh(sec)

            session.add(SectionMeeting(
                section_id=sec.id, meeting_sequence=0,
                time_slot_id=2, room_id="SCI-201",
            ))
        session.commit()

        recs = analyze_schedule(session, schedule.id)
        overload = [r for r in recs if r.type == RecommendationType.FACULTY_OVERLOAD]
        assert len(overload) == 1
        assert overload[0].priority > 0
        assert "Jones" in overload[0].description


def test_recommend_time_conflict(import_ready_db):
    """High-conflict pair in overlapping slots → TIME_CONFLICT."""
    with import_ready_db.get_session() as session:
        # CSCI1301 and CSCI2301 share students (S001-S005 need both)
        # Put both in the same MWF 08:00 slot → they overlap
        schedule = ScheduleVersion(
            name="Test", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        sec1 = Section(
            schedule_id=schedule.id, crn="TC001",
            course_code="CSCI1301", section_number="001",
            capacity=30,
        )
        sec2 = Section(
            schedule_id=schedule.id, crn="TC002",
            course_code="CSCI2301", section_number="001",
            capacity=30,
        )
        session.add(sec1)
        session.add(sec2)
        session.commit()
        session.refresh(sec1)
        session.refresh(sec2)

        session.add(SectionMeeting(
            section_id=sec1.id, meeting_sequence=0,
            time_slot_id=1, room_id="SCI-101",
        ))
        session.add(SectionMeeting(
            section_id=sec2.id, meeting_sequence=0,
            time_slot_id=1, room_id="SCI-201",
        ))
        session.commit()

        # Use a low threshold to catch the conflict
        recs = analyze_schedule(session, schedule.id, conflict_threshold=1.0)
        time_conflicts = [r for r in recs if r.type == RecommendationType.TIME_CONFLICT]
        assert len(time_conflicts) >= 1


def test_recommend_priority_ordering(import_ready_db):
    """Results sorted by priority descending."""
    with import_ready_db.get_session() as session:
        # Empty schedule → all demands unscheduled → multiple recommendations
        schedule = ScheduleVersion(
            name="Test", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        recs = analyze_schedule(session, schedule.id)
        assert len(recs) >= 2
        for i in range(len(recs) - 1):
            assert recs[i].priority >= recs[i + 1].priority


def test_recommend_clean_schedule(import_ready_db):
    """Well-balanced schedule → no critical recommendations (no UNSCHEDULED/INSUFFICIENT)."""
    with import_ready_db.get_session() as session:
        schedule = ScheduleVersion(
            name="Clean", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        # Schedule all demanded courses with sufficient capacity in different slots
        section_data = [
            ("CL001", "CSCI1301", "001", 40, 1, "SCI-201"),
            ("CL002", "CSCI2301", "001", 40, 3, "SCI-201"),
            ("CL003", "BIOL1401", "001", 40, 2, "SCI-201"),
            ("CL004", "MATH1301", "001", 40, 2, "SCI-101"),
        ]
        for crn, code, sec_num, cap, ts, room in section_data:
            s = Section(
                schedule_id=schedule.id, crn=crn,
                course_code=code, section_number=sec_num,
                capacity=cap,
            )
            session.add(s)
            session.commit()
            session.refresh(s)

            session.add(SectionMeeting(
                section_id=s.id, meeting_sequence=0,
                time_slot_id=ts, room_id=room,
            ))
        session.commit()

        recs = analyze_schedule(session, schedule.id)
        critical = [
            r for r in recs
            if r.type in (
                RecommendationType.UNSCHEDULED_COURSE,
                RecommendationType.INSUFFICIENT_SECTIONS,
            )
        ]
        assert len(critical) == 0


def test_no_demands_no_recommendations(db_manager):
    """Empty demands → empty recommendations list."""
    with db_manager.get_session() as session:
        schedule = ScheduleVersion(
            name="Empty", created_at=datetime.fromisoformat("2025-01-01T00:00:00"),
            semester="2025SP", status="draft", solver_stats={},
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        recs = analyze_schedule(session, schedule.id)
        assert recs == []
