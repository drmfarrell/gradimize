"""Class-size caps and floating ("room") capacity mode.

Covers the feature end to end: the max_class_size catalog column, per-course
caps in fixed mode, and capacity_mode "room" where section sizes float up to
the assigned room's capacity (bounded by course/global caps) and the stored
section capacity reflects the room actually chosen.
"""

import tempfile
from pathlib import Path

import pytest

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.core.ingest import DataIngestor
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    Faculty,
    Room,
    RoomType,
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


def _populate(session, *, course_cap=None, room_capacity=12, n_students=12):
    """One course, one room, one slot, n students demanding the course."""
    session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                         start_time="09:00", end_time="09:50", duration_minutes=50,
                         slot_type="MWF_50"))
    session.add(Room(id="R1", building="Test", number="1", capacity=room_capacity,
                     room_type=RoomType.LECTURE))
    session.add(Course(code="CSCI101", title="Intro", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"],
                       max_class_size=course_cap))
    session.add(Faculty(id="F1", name="Prof A", email="a@test.com", max_credits=12,
                        approved_courses=["CSCI101"]))
    for i in range(n_students):
        session.add(Student(id=f"S{i}", name=f"Student {i}", email=f"s{i}@test.com",
                            class_standing=ClassStanding.FR, credits_earned=0,
                            major="CSCI", catalog_year=2024))
        session.add(StudentDemand(student_id=f"S{i}", course_code="CSCI101",
                                  urgency_score=15.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
    session.commit()


def _solve(session, config):
    conflict_matrix = ConflictMatrix(session)
    conflict_matrix.build_matrix()
    config.solver_settings["max_time_seconds"] = 30
    engine = SchedulingEngine(session, conflict_matrix, config)
    sections = engine.create_sections("2025SP")
    result = engine.solve(sections, "Class Size Test", "2025SP")
    assert result.status in ("optimal", "feasible")
    return result


def test_per_course_cap_fixed_mode(db_manager):
    """A course's max_class_size overrides default_capacity in both directions."""
    with db_manager.get_session() as session:
        _populate(session, course_cap=10, room_capacity=40, n_students=15)
        # Second course capped ABOVE the default (auditorium-style), with its
        # own slot, room, and instructor so the model stays feasible.
        session.add(Course(code="PSYC101", title="Psych", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"], max_class_size=60))
        session.add(TimeSlot(id=2, name="MWF_1000", pattern="MWF",
                             days=["M", "W", "F"], start_time="10:00",
                             end_time="10:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="R2", building="Test", number="2", capacity=80,
                         room_type=RoomType.LECTURE))
        session.add(Faculty(id="F2", name="Prof B", email="b@test.com",
                            max_credits=12, approved_courses=["PSYC101"]))
        session.commit()

        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()
        engine = SchedulingEngine(session, conflict_matrix, SchedulerConfig())
        sections = engine.create_sections("2025SP")

        caps = {s.course_code: s.capacity for s in sections}
        assert caps["CSCI101"] == 10, "per-course cap below default must apply"
        assert caps["PSYC101"] == 60, "per-course cap above default must apply"

        config = SchedulerConfig()
        result = _solve(session, config)
        per_section: dict[int, int] = {}
        for e in result.enrollments:
            per_section[e.section_id] = per_section.get(e.section_id, 0) + 1
        assert per_section, "solver should enroll students"
        for count in per_section.values():
            assert count <= 10, "enrollment must respect the per-course cap"


def test_room_mode_size_floats_to_room(db_manager):
    """capacity_mode 'room': a 12-seat room fills past the 5-seat default,
    and the stored section capacity is the room's size."""
    with db_manager.get_session() as session:
        _populate(session, room_capacity=12, n_students=12)

        config = SchedulerConfig()
        config.section_settings["default_capacity"] = 5
        config.section_settings["capacity_mode"] = "room"
        result = _solve(session, config)

        assert len(result.enrollments) == 12, (
            "floating capacity should let all 12 students into the 12-seat room"
        )
        assert result.sections[0].capacity == 12, (
            "stored capacity must reflect the assigned room"
        )


def test_room_mode_respects_course_cap(db_manager):
    """Even in room mode, the course's max_class_size stays a hard ceiling."""
    with db_manager.get_session() as session:
        _populate(session, course_cap=8, room_capacity=12, n_students=12)

        config = SchedulerConfig()
        config.section_settings["capacity_mode"] = "room"
        result = _solve(session, config)

        assert len(result.enrollments) == 8
        assert result.sections[0].capacity == 8


def test_room_mode_global_ceiling(db_manager):
    """The global max_class_size caps floating size when a course has no cap."""
    with db_manager.get_session() as session:
        _populate(session, room_capacity=12, n_students=12)

        config = SchedulerConfig()
        config.section_settings["capacity_mode"] = "room"
        config.section_settings["max_class_size"] = 6
        result = _solve(session, config)

        assert len(result.enrollments) == 6
        assert result.sections[0].capacity == 6


def test_fixed_mode_unchanged_without_caps(db_manager):
    """Default config keeps the classic behavior: default_capacity bound."""
    with db_manager.get_session() as session:
        _populate(session, room_capacity=40, n_students=35)
        # 35 demands spawn two sections; give the second one a slot to live in.
        session.add(TimeSlot(id=2, name="MWF_1000", pattern="MWF",
                             days=["M", "W", "F"], start_time="10:00",
                             end_time="10:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.commit()

        config = SchedulerConfig()  # fixed mode, default_capacity 30
        result = _solve(session, config)

        per_section: dict[int, int] = {}
        for e in result.enrollments:
            per_section[e.section_id] = per_section.get(e.section_id, 0) + 1
        for count in per_section.values():
            assert count <= 30


def test_no_double_enrollment_when_seats_are_plentiful(db_manager):
    """A student never occupies two sections of the same course.

    With surplus capacity the objective would otherwise double-count a
    student's urgency by enrolling them in sibling sections (caught in the
    Fall'26 back-test rehearsal: +592 phantom enrollments in room mode)."""
    with db_manager.get_session() as session:
        # 10 students, TWO forced sections, seats for 24 in each: plenty of
        # spare capacity to tempt a double-enrollment.
        _populate(session, room_capacity=24, n_students=10)
        session.add(TimeSlot(id=2, name="MWF_1000", pattern="MWF",
                             days=["M", "W", "F"], start_time="10:00",
                             end_time="10:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(Room(id="R2", building="Test", number="2", capacity=24,
                         room_type=RoomType.LECTURE))
        session.commit()

        conflict_matrix = ConflictMatrix(session)
        conflict_matrix.build_matrix()
        config = SchedulerConfig()
        config.section_settings["capacity_mode"] = "room"
        config.solver_settings["max_time_seconds"] = 30
        engine = SchedulingEngine(session, conflict_matrix, config)
        sections = engine.create_sections("2025SP", sections_per_course={"CSCI101": 2})
        result = engine.solve(sections, "Double Enroll Test", "2025SP")
        assert result.status in ("optimal", "feasible")

        assert len(result.enrollments) == 10, (
            f"10 demands must yield exactly 10 enrollments, got "
            f"{len(result.enrollments)} — duplicate (student, course) rows?"
        )


def test_max_class_size_csv_column(db_manager, tmp_path):
    """catalog.csv max_class_size: parsed, blank = None, junk = error + None."""
    (tmp_path / "catalog.csv").write_text(
        "code,title,credits,structure_type,max_class_size\n"
        "GOOD101,Good,3,STANDALONE,24\n"
        "FLOAT101,Spreadsheet,3,STANDALONE,25.0\n"
        "BLANK101,Blank,3,STANDALONE,\n"
        "JUNK101,Junk,3,STANDALONE,lots\n"
        "NEG101,Negative,3,STANDALONE,-4\n"
    )
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, str(tmp_path))
        ingestor.load_courses()

        assert session.get(Course, "GOOD101").max_class_size == 24
        assert session.get(Course, "FLOAT101").max_class_size == 25
        assert session.get(Course, "BLANK101").max_class_size is None
        assert session.get(Course, "JUNK101").max_class_size is None
        assert session.get(Course, "NEG101").max_class_size is None
        joined = " ".join(ingestor.errors)
        assert "JUNK101" in joined and "NEG101" in joined


def test_capacity_mode_validated_from_yaml(tmp_path):
    """An unknown capacity_mode in YAML falls back to 'fixed' with a warning."""
    cfg_file = tmp_path / "constraints.yaml"
    cfg_file.write_text(
        "section_settings:\n"
        "  capacity_mode: sideways\n"
        "  max_class_size: 45\n"
    )
    config = SchedulerConfig(cfg_file)
    assert config.section_settings["capacity_mode"] == "fixed"
    assert config.section_settings["max_class_size"] == 45
