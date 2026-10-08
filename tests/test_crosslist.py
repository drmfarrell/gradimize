"""Cross-listing: one physical class under multiple course codes."""

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


def _seed_pair(session, *, hist_demands=5, afam_demands=5, room_capacity=8):
    """HIST201/AFAM201 cross-listed; students demand their own code."""
    for i in (1, 2):
        session.add(TimeSlot(id=i, name=f"MWF_{8 + i:02d}00", pattern="MWF",
                             days=["M", "W", "F"], start_time=f"{8 + i:02d}:00",
                             end_time=f"{8 + i:02d}:50", duration_minutes=50,
                             slot_type="MWF_50"))
    for r in ("R1", "R2"):
        session.add(Room(id=r, building="B", number=r[-1], capacity=room_capacity,
                         room_type=RoomType.LECTURE))
    for code in ("HIST201", "AFAM201"):
        session.add(Course(code=code, title="African American History", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"],
                           crosslist_group="XL-HIST201"))
    session.add(Faculty(id="F1", name="Prof", email="p@t.co", max_credits=12,
                        approved_courses=["HIST201", "AFAM201"]))
    n = 0
    for code, count in (("HIST201", hist_demands), ("AFAM201", afam_demands)):
        for _ in range(count):
            n += 1
            session.add(Student(id=f"S{n}", name=f"Student {n}", email="",
                                class_standing=ClassStanding.SO, credits_earned=30,
                                major="HIST", catalog_year=2024))
            session.add(StudentDemand(student_id=f"S{n}", course_code=code,
                                      urgency_score=20.0, is_required=True,
                                      is_graduation_critical=False, reason="t"))
    session.commit()


def _solve(session):
    cm = ConflictMatrix(session)
    cm.build_matrix()
    config = SchedulerConfig()
    config.solver_settings["max_time_seconds"] = 30
    engine = SchedulingEngine(session, cm, config)
    sections = engine.create_sections("2026FA")
    result = engine.solve(sections, "XL Test", "2026FA")
    assert result.status in ("optimal", "feasible")
    return sections, result


def test_ingest_crosslist_column(db_manager, tmp_path):
    (tmp_path / "catalog.csv").write_text(
        "code,title,credits,structure_type,crosslist_group\n"
        "HIST201,AAH,3,STANDALONE,XL1\n"
        "AFAM201,AAH,3,STANDALONE,XL1\n"
        "MATH101,Alg,3,STANDALONE,\n"
    )
    with db_manager.get_session() as session:
        DataIngestor(session, str(tmp_path)).load_courses()
        assert session.get(Course, "HIST201").crosslist_group == "XL1"
        assert session.get(Course, "AFAM201").crosslist_group == "XL1"
        assert session.get(Course, "MATH101").crosslist_group is None


def test_pooled_demand_sizes_the_group_once(db_manager):
    """3 + 4 pooled = 7 students -> ONE section per member, paired numbers."""
    with db_manager.get_session() as session:
        _seed_pair(session, hist_demands=3, afam_demands=4, room_capacity=30)
        cm = ConflictMatrix(session)
        cm.build_matrix()
        engine = SchedulingEngine(session, cm, SchedulerConfig())
        sections = engine.create_sections("2026FA")

        by_code = {}
        for s in sections:
            by_code.setdefault(s.course_code, []).append(s)
        assert len(by_code["HIST201"]) == 1
        assert len(by_code["AFAM201"]) == 1
        assert (by_code["HIST201"][0].section_number
                == by_code["AFAM201"][0].section_number)


def test_shared_placement_instructor_and_joint_seats(db_manager):
    """The pair lands in ONE room/slot with ONE instructor, and combined
    enrollment respects that single room's capacity."""
    with db_manager.get_session() as session:
        _seed_pair(session, hist_demands=5, afam_demands=5, room_capacity=8)
        sections, result = _solve(session)

        placements = {}
        for m in result.meetings:
            placements[m.section_id] = (m.time_slot_id, m.room_id)
        secs = {s.course_code: s for s in result.sections}
        hist, afam = secs["HIST201"], secs["AFAM201"]

        assert placements[hist.id] == placements[afam.id], (
            "cross-listed pair must share slot AND room"
        )
        assert hist.faculty_id == afam.faculty_id, "one instructor for the pair"

        total = len(result.enrollments)
        assert total == 8, (
            f"joint enrollment must fill exactly the one shared 8-seat room "
            f"(got {total}; 16 would mean the pair was treated as two rooms)"
        )


def test_doctor_flags_inconsistent_groups(db_manager):
    from src.core.doctor import run_health_checks

    with db_manager.get_session() as session:
        session.add(Course(code="AAA101", title="A", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"], crosslist_group="XLA"))
        session.add(Course(code="BBB101", title="B", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["SEMINAR"],
                           valid_slot_types=["TR_75"], crosslist_group="XLA"))
        session.add(Course(code="CCC101", title="C", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"], crosslist_group="LONER"))
        session.commit()
        results = {r.name: r for r in run_health_checks(session, deep=False)}

    xl = results["crosslist-consistency"]
    assert xl.status == "warn"
    joined = " ".join(xl.details)
    assert "valid_slot_types" in joined
    assert "required_room_types" in joined
    assert "LONER" in joined
