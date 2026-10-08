"""Tests for the deferred model/DDL items in src/database/models.py and the
guarded in-place migrations in src/database/connection.py:

- created_at: ISO string -> real datetime columns, old string rows still read.
- Section.needs_final: Optional[bool] -> bool, NULL backfilled to True.
- Unique constraints on (section_id, student_id) enrollments and
  (section_id, meeting_sequence) meetings, with a duplicate-safe migration.
"""

import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from src.database.connection import _apply_lightweight_migrations
from src.database.models import ScheduleVersion, SectionEnrollment, SectionMeeting


def test_created_at_old_string_row_still_reads(db_manager):
    """A row written under the old isoformat-string regime (pre-migration)
    is still readable as a proper datetime through the model."""
    with db_manager.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO scheduleversion (name, created_at, semester, status, solver_stats) "
            "VALUES ('Legacy', '2026-01-01T00:00:00', '2026SP', 'draft', '{}')"
        ))

    with db_manager.get_session() as session:
        sched = session.exec(select(ScheduleVersion).where(ScheduleVersion.name == "Legacy")).one()
        assert sched.created_at.isoformat() == "2026-01-01T00:00:00"


def test_created_at_ordering_matches_old_lexicographic_order(db_manager):
    """Datetime ordering must agree with the old lexicographic isoformat
    ordering: insert out of order, sort ascending, and check chronology."""
    with db_manager.engine.begin() as conn:
        for name, ts in [
            ("C", "2026-03-01T00:00:00"),
            ("A", "2026-01-01T00:00:00"),
            ("B", "2026-02-01T00:00:00"),
        ]:
            conn.execute(text(
                "INSERT INTO scheduleversion (name, created_at, semester, status, solver_stats) "
                f"VALUES ('{name}', '{ts}', '2026SP', 'draft', '{{}}')"
            ))

    with db_manager.get_session() as session:
        rows = session.exec(select(ScheduleVersion).order_by(ScheduleVersion.created_at)).all()
        assert [r.name for r in rows] == ["A", "B", "C"]


def test_needs_final_null_backfilled_to_true(db_manager):
    """A NULL needs_final (old nullable Optional[bool] regime) is backfilled
    to True, since finals.py only ever skipped on an explicit False."""
    with db_manager.engine.begin() as conn:
        # Recreate `section` matching the pre-migration nullable DDL so we
        # can actually store a NULL (the current model's column is NOT NULL).
        conn.exec_driver_sql("DROP TABLE section")
        conn.exec_driver_sql(
            "CREATE TABLE section (id INTEGER PRIMARY KEY, schedule_id INTEGER, "
            "crn VARCHAR, course_code VARCHAR, section_number VARCHAR, "
            "capacity INTEGER, is_honors BOOLEAN, faculty_id VARCHAR, "
            "is_pinned BOOLEAN, pin_reason VARCHAR, needs_final BOOLEAN)"
        )
        conn.execute(text(
            "INSERT INTO section (schedule_id, crn, course_code, section_number, capacity, "
            "is_honors, is_pinned, needs_final) "
            "VALUES (1, 'C1', 'BIO101', '001', 30, 0, 0, NULL)"
        ))

    _apply_lightweight_migrations(db_manager.engine)

    with db_manager.engine.begin() as conn:
        value = conn.exec_driver_sql(
            "SELECT needs_final FROM section WHERE crn = 'C1'"
        ).scalar()
        assert value == 1


def test_unique_index_rejects_duplicate_enrollment(db_manager):
    """Fresh DB: the unique (section_id, student_id) constraint rejects a
    duplicate insert."""
    with Session(db_manager.engine) as session:
        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        session.commit()

        session.add(SectionEnrollment(section_id=1, student_id="S1"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_unique_index_rejects_duplicate_meeting(db_manager):
    """Fresh DB: the unique (section_id, meeting_sequence) constraint rejects
    a duplicate insert."""
    with Session(db_manager.engine) as session:
        session.add(SectionMeeting(section_id=1, meeting_sequence=0, time_slot_id=1, room_id="R1"))
        session.commit()

        session.add(SectionMeeting(section_id=1, meeting_sequence=0, time_slot_id=2, room_id="R2"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_migration_skips_unique_index_on_preexisting_duplicates(tmp_path, caplog):
    """A DB seeded with a deliberate duplicate (as if from before the
    constraint existed) must not crash the migration, and stays usable
    (further duplicates are still allowed since the index was skipped)."""
    from sqlalchemy import create_engine

    db_path = tmp_path / "dupe.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO sectionenrollment (section_id, student_id, is_confirmed) "
            "VALUES (1, 'S1', 0)"
        ))
        conn.execute(text(
            "INSERT INTO sectionenrollment (section_id, student_id, is_confirmed) "
            "VALUES (1, 'S1', 0)"
        ))

    with caplog.at_level(logging.WARNING):
        _apply_lightweight_migrations(engine)  # must not raise

    assert any("ux_sectionenrollment_section_student" in r.message for r in caplog.records)

    # DB still usable: the skipped index means a third duplicate is still
    # accepted without error.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO sectionenrollment (section_id, student_id, is_confirmed) "
            "VALUES (1, 'S1', 0)"
        ))
        count = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM sectionenrollment WHERE section_id=1 AND student_id='S1'"
        ).scalar()
        assert count == 3

    # The other (non-duplicated) unique index was still created normally.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO sectionmeeting (section_id, meeting_sequence, time_slot_id, room_id) "
            "VALUES (1, 0, 1, 'R1')"
        ))
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO sectionmeeting (section_id, meeting_sequence, time_slot_id, room_id) "
                "VALUES (1, 0, 2, 'R2')"
            ))

    engine.dispose()
