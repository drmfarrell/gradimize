"""Tests for data ingestion."""

import tempfile
from pathlib import Path

import pytest

from src.core.ingest import DataIngestor
from src.database.connection import DatabaseManager
from src.database.models import Course, Room, RoomType, StructureType, Student


@pytest.fixture
def db_manager():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield manager

    # Cleanup
    db_path.unlink(missing_ok=True)


@pytest.fixture
def sample_data_dir(tmp_path):
    """Create sample CSV files for testing."""
    # Create catalog.csv
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        "code,title,credits,structure_type,linked_course_code,required_room_types,valid_slot_types,min_enrollment\n"
        "TEST101,Test Course,3,STANDALONE,,LECTURE,MWF_50,5\n"
        "TEST201,Advanced Test,3,PARENT,,LECTURE,TR_75,5\n"
        "TEST201L,Test Lab,1,CHILD,TEST201,LAB_DRY,LAB_150,5\n"
    )

    # Create rooms.csv
    rooms = tmp_path / "rooms.csv"
    rooms.write_text(
        "id,building,number,capacity,room_type,equipment,is_active\n"
        "ROOM-101,Test Building,101,30,LECTURE,projector,True\n"
        "ROOM-201,Test Building,201,20,LAB_DRY,computers,True\n"
    )

    # Create students.csv
    students = tmp_path / "students.csv"
    students.write_text(
        "id,name,email,class_standing,credits_earned,major,minor,is_honors,is_athlete,catalog_year\n"
        "S00001,Test Student,test@example.com,FR,15,CSCI,,False,False,2024\n"
        "S00002,Another Student,another@example.com,SR,90,BIOL,CHEM,True,False,2024\n"
    )

    # Create faculty.csv
    faculty = tmp_path / "faculty.csv"
    faculty.write_text(
        "id,name,email,max_credits,approved_courses,unavailable_slots,preferences\n"
        "F001,Test Faculty,faculty@example.com,12,TEST101,\"\",\"\"\n"
    )

    # Create prerequisites.csv
    prereqs = tmp_path / "prerequisites.csv"
    prereqs.write_text(
        "course_code,prereq_code,min_grade,is_corequisite\n"
        "TEST201L,TEST201,D,True\n"
    )

    # Create course_history.csv
    history = tmp_path / "course_history.csv"
    history.write_text(
        "student_id,course_code,grade,semester,credits_earned\n"
    )

    # Create degree_requirements.csv
    requirements = tmp_path / "degree_requirements.csv"
    requirements.write_text(
        "major,course_code,is_required,category,catalog_year\n"
        "CSCI,TEST101,True,core,2024\n"
    )

    return tmp_path


def test_load_courses(db_manager, sample_data_dir):
    """Test loading courses from CSV."""
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, sample_data_dir)
        courses = ingestor.load_courses()

        assert len(courses) == 3

        test101 = session.get(Course, "TEST101")
        assert test101 is not None
        assert test101.title == "Test Course"
        assert test101.credits == 3
        assert test101.structure_type == StructureType.STANDALONE

        test201l = session.get(Course, "TEST201L")
        assert test201l is not None
        assert test201l.structure_type == StructureType.CHILD
        assert test201l.linked_course_code == "TEST201"


def test_load_rooms(db_manager, sample_data_dir):
    """Test loading rooms from CSV."""
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, sample_data_dir)
        rooms = ingestor.load_rooms()

        assert len(rooms) == 2

        room101 = session.get(Room, "ROOM-101")
        assert room101 is not None
        assert room101.capacity == 30
        assert room101.room_type == RoomType.LECTURE


def test_load_students(db_manager, sample_data_dir):
    """Test loading students from CSV."""
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, sample_data_dir)
        students = ingestor.load_students()

        assert len(students) == 2

        student1 = session.get(Student, "S00001")
        assert student1 is not None
        # Student identifier columns (incl. name) are dropped at import by
        # policy — a placeholder is stored even when the CSV carries a name.
        assert student1.name == "Student S00001"
        assert student1.major == "CSCI"


def test_load_all(db_manager, sample_data_dir, tmp_path):
    """Test loading all data at once."""
    # Create time_slots.yaml
    time_slots_config = tmp_path / "time_slots.yaml"
    time_slots_config.write_text(
        """
slot_types:
  MWF_50:
    pattern: MWF
    days: [M, W, F]
    duration: 50
  TR_75:
    pattern: TR
    days: [T, R]
    duration: 75
  LAB_150:
    pattern: single
    days: [T, R]
    duration: 170

slots:
  - type: MWF_50
    start: "08:00"
  - type: TR_75
    start: "09:30"
  - type: LAB_150
    days: [T]
    start: "14:00"
"""
    )

    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, sample_data_dir)
        result = ingestor.load_all(time_slots_config)

        assert result["courses"] == 3
        assert result["rooms"] == 2
        assert result["students"] == 2
        assert result["time_slots"] == 3
