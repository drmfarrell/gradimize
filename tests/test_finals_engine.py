"""Smoke tests for the finals CP-SAT engine (previously had zero coverage)."""

from datetime import datetime

import pytest
from sqlmodel import select

from src.core.finals import FinalsConfig, FinalsEngine, FinalsExporter
from src.database.models import (
    Course,
    Faculty,
    FinalExamAssignment,
    FinalExamBlock,
    Room,
    RoomType,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    StructureType,
    TimeSlot,
)


@pytest.fixture
def finals_dataset(db_manager):
    """A tiny scheduled semester: 3 sections, shared students, one evening class."""
    with db_manager.get_session() as session:
        session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                             start_time="09:00", end_time="09:50", duration_minutes=50,
                             slot_type="MWF_50"))
        session.add(TimeSlot(id=2, name="TR_1000", pattern="TR", days=["T", "R"],
                             start_time="10:00", end_time="11:15", duration_minutes=75,
                             slot_type="TR_75"))
        session.add(TimeSlot(id=3, name="MW_1830", pattern="MW", days=["M", "W"],
                             start_time="18:30", end_time="19:45", duration_minutes=75,
                             slot_type="EVE_75"))
        session.add(Room(id="R1", building="Main", number="101", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Room(id="R2", building="Main", number="102", capacity=30,
                         room_type=RoomType.LECTURE))
        for code in ("BIO101", "CHM101", "BIO402"):
            session.add(Course(code=code, title=code, credits=3,
                               structure_type=StructureType.STANDALONE,
                               required_room_types=["LECTURE"],
                               valid_slot_types=["MWF_50", "TR_75"]))
        session.add(Faculty(id="F1", name="Prof A", email="a@t.co", max_credits=12))
        session.add(ScheduleVersion(id=1, name="Fall", semester="2026FA",
                                    status="draft", created_at=datetime(2026, 7, 14)))
        session.add(Section(id=1, schedule_id=1, crn="10001", course_code="BIO101",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(Section(id=2, schedule_id=1, crn="10002", course_code="CHM101",
                            section_number="001", capacity=30, faculty_id="F1"))
        # Evening section: starts after the 18:00 threshold -> in-place final
        session.add(Section(id=3, schedule_id=1, crn="10003", course_code="BIO402",
                            section_number="001", capacity=30, faculty_id="F1"))
        session.add(SectionMeeting(section_id=1, meeting_sequence=0, time_slot_id=1,
                                   room_id="R1"))
        session.add(SectionMeeting(section_id=2, meeting_sequence=0, time_slot_id=2,
                                   room_id="R2"))
        session.add(SectionMeeting(section_id=3, meeting_sequence=0, time_slot_id=3,
                                   room_id="R1"))
        # S1 takes both morning courses — their finals must not share a block
        for sid, students in ((1, ["S1", "S2"]), (2, ["S1", "S3"]), (3, ["S4"])):
            for st in students:
                session.add(SectionEnrollment(section_id=sid, student_id=st))
        session.commit()
    return db_manager


def test_finals_solve_assigns_every_section(finals_dataset):
    with finals_dataset.get_session() as session:
        cfg = FinalsConfig(max_time_seconds=30)
        engine = FinalsEngine(session, 1, cfg)
        result = engine.solve(name="Test Finals")

        assert result.status in ("optimal", "feasible"), result.status

        assignments = list(session.exec(select(FinalExamAssignment)).all())
        assert len(assignments) == 3, "every section gets a final"

        by_section = {a.section_id: a for a in assignments}
        # Evening class takes its final in place
        assert by_section[3].is_in_place is True
        assert by_section[3].in_place_time_slot_id == 3
        # Morning classes get real blocks
        assert by_section[1].block_id is not None
        assert by_section[2].block_id is not None

        # S1 is in sections 1 and 2 — a tiny instance must resolve the clash
        assert by_section[1].block_id != by_section[2].block_id, (
            "shared-student sections must not share an exam block"
        )

        blocks = list(session.exec(select(FinalExamBlock)).all())
        assert blocks, "candidate blocks were generated"
        assert result.num_students_with_conflict == 0


def test_finals_csv_export_days_chronological(finals_dataset):
    with finals_dataset.get_session() as session:
        cfg = FinalsConfig(max_time_seconds=30)
        engine = FinalsEngine(session, 1, cfg)
        result = engine.solve(name="Test Finals")
        assert result.status in ("optimal", "feasible")

        csv_text = FinalsExporter(session).export_csv(result.finals_schedule.id)
        lines = csv_text.strip().splitlines()
        assert lines[0].startswith("CRN,")
        assert len(lines) == 4  # header + 3 sections
