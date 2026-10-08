"""Registrations → demand importer, including status-code semantics."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.registrations import import_registrations_as_demand
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    StructureType,
    Student,
    StudentDemand,
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
def seeded(db_manager):
    with db_manager.get_session() as session:
        session.add(Course(code="CHEM101", title="Chem", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Course(code="MATH101", title="Math", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(DegreeRequirement(major="CHEM", course_code="CHEM101",
                                      is_required=True, category="core",
                                      catalog_year=2024))
        for sid in ("S1", "S2", "S3"):
            session.add(Student(id=sid, name=sid, email="",
                                class_standing=ClassStanding.SO,
                                credits_earned=30, major="CHEM",
                                catalog_year=2024))
        session.commit()
    return db_manager


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "registrations.csv"
    p.write_text(text)
    return p


def test_without_status_column_every_row_counts(seeded, tmp_path):
    src = _write(tmp_path,
                 "student_id,term,crn,subject,course_number,section\n"
                 "S1,202610,20001,CHEM,101,001\n"
                 "S2,202610,20002,MATH,101,001\n")
    with seeded.get_session() as session:
        stats = import_registrations_as_demand(session, src)
        demands = list(session.exec(select(StudentDemand)).all())
    assert stats["demands_created"] == 2
    assert stats["waitlisted_as_demand"] == 0
    assert stats["skipped_by_status"] == {}
    assert {(d.student_id, d.course_code) for d in demands} == {
        ("S1", "CHEM101"), ("S2", "MATH101")
    }


def test_status_codes_filter_drops_and_keep_waitlist(seeded, tmp_path):
    """R* and WL* are demand (waitlist = demand the old schedule failed to
    seat); drops/withdrawals/audits are excluded and tallied."""
    src = _write(tmp_path,
                 "student_id,term,crn,subject,course_number,section,status\n"
                 "S1,202610,20001,CHEM,101,001,RE\n"
                 "S2,202610,20001,CHEM,101,001,WL\n"
                 "S3,202610,20001,CHEM,101,001,DD\n"
                 "S3,202610,20002,MATH,101,001,AU\n")
    with seeded.get_session() as session:
        stats = import_registrations_as_demand(session, src)
        demands = list(session.exec(select(StudentDemand)).all())
    assert stats["demands_created"] == 2
    assert stats["waitlisted_as_demand"] == 1
    assert stats["skipped_by_status"] == {"DD": 1, "AU": 1}
    by_student = {d.student_id: d for d in demands}
    assert set(by_student) == {"S1", "S2"}
    assert by_student["S1"].reason.startswith("Registered")
    assert by_student["S2"].reason.startswith("Waitlisted")


def test_unknown_student_and_course_still_skipped(seeded, tmp_path):
    src = _write(tmp_path,
                 "student_id,term,crn,subject,course_number,section,status\n"
                 "GHOST,202610,20001,CHEM,101,001,RE\n"
                 "S1,202610,20009,BASKET,101,001,RE\n")
    with seeded.get_session() as session:
        stats = import_registrations_as_demand(session, src)
    assert stats["demands_created"] == 0
    assert stats["skipped_unknown_student"] == 1
    assert stats["skipped_unknown_course"] == 1
