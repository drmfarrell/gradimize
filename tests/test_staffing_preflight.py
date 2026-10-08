"""Staffing pre-flight: structural must-staff bottlenecks named before solving.

Every section whose course has approved faculty must be staffed by one of
them, and one instructor teaches at most one section per slot — so a course
group can never need more section-meetings than its instructors have usable
slots. The pre-flight warns on certain impossibilities (never maybes) so an
"unknown" solver verdict stops being a mystery.
"""

import tempfile
from pathlib import Path

import pytest

from src.core.conflicts import ConflictMatrix
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.database.connection import DatabaseManager
from src.database.models import (
    Course,
    Faculty,
    Room,
    RoomType,
    StructureType,
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


def _slots(session, n):
    for i in range(n):
        session.add(TimeSlot(id=i + 1, name=f"MWF_{8+i:02d}00", pattern="MWF",
                             days=["M", "W", "F"], start_time=f"{8+i:02d}:00",
                             end_time=f"{8+i:02d}:50", duration_minutes=50,
                             slot_type="MWF_50"))


def _course(code):
    return Course(code=code, title=code, credits=3,
                  structure_type=StructureType.STANDALONE,
                  required_room_types=["LECTURE"], valid_slot_types=["MWF_50"])


def _engine(session):
    cm = ConflictMatrix(session)
    cm.build_matrix()
    return SchedulingEngine(session, cm, SchedulerConfig())


def test_overloaded_dedicated_group_warns(db_manager):
    """4 sections for a course whose ONLY instructor has 2 usable slots."""
    with db_manager.get_session() as session:
        _slots(session, 2)
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Room(id="R2", building="B", number="2", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(_course("OVR101"))
        session.add(Faculty(id="FONLY", name="Only Prof", email="o@t.co",
                            max_credits=12, approved_courses=["OVR101"]))
        session.commit()

        engine = _engine(session)
        sections = engine.create_sections("2025SP", sections_per_course={"OVR101": 4})
        warnings = engine._staffing_preflight(sections)

        assert len(warnings) == 1
        assert "OVR101" in warnings[0] and "FONLY" in warnings[0]
        assert "need 4" in warnings[0]


def test_unrestricted_pool_absorbs_overflow(db_manager):
    """Same overload, but an any-course instructor covers the deficit."""
    with db_manager.get_session() as session:
        _slots(session, 2)
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Room(id="R2", building="B", number="2", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(_course("OVR101"))
        session.add(Faculty(id="FONLY", name="Only Prof", email="o@t.co",
                            max_credits=12, approved_courses=["OVR101"]))
        session.add(Faculty(id="FPOOL", name="Adjunct", email="a@t.co",
                            max_credits=12, approved_courses=[]))
        session.commit()

        engine = _engine(session)
        sections = engine.create_sections("2025SP", sections_per_course={"OVR101": 4})
        assert engine._staffing_preflight(sections) == []


def test_shared_pool_cannot_cover_every_group(db_manager):
    """Each group fits WITH borrowing, but total borrowing exceeds the pool."""
    with db_manager.get_session() as session:
        _slots(session, 2)
        for i in (1, 2):
            session.add(Room(id=f"R{i}", building="B", number=str(i), capacity=30,
                             room_type=RoomType.LECTURE))
        session.add(_course("AAA101"))
        session.add(_course("BBB101"))
        session.add(Faculty(id="FA", name="A", email="a@t.co", max_credits=12,
                            approved_courses=["AAA101"]))
        session.add(Faculty(id="FB", name="B", email="b@t.co", max_credits=12,
                            approved_courses=["BBB101"]))
        session.add(Faculty(id="FPOOL", name="Adjunct", email="p@t.co",
                            max_credits=12, approved_courses=[]))
        session.commit()

        engine = _engine(session)
        # AAA needs 4 (own cap 2, deficit 2); BBB needs 3 (deficit 1).
        # Pool covers only 2 — jointly impossible even though each group
        # alone would fit with borrowing.
        sections = engine.create_sections(
            "2025SP", sections_per_course={"AAA101": 4, "BBB101": 3}
        )
        warnings = engine._staffing_preflight(sections)
        assert len(warnings) == 1
        assert "shared" in warnings[0] or "pool" in warnings[0]


def test_solve_carries_staffing_warnings(db_manager):
    """SolverResult.staffing_warnings is populated on an impossible instance."""
    with db_manager.get_session() as session:
        _slots(session, 2)
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(Room(id="R2", building="B", number="2", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(_course("OVR101"))
        session.add(Faculty(id="FONLY", name="Only Prof", email="o@t.co",
                            max_credits=12, approved_courses=["OVR101"]))
        session.commit()

        engine = _engine(session)
        config = engine.config
        config.solver_settings["max_time_seconds"] = 20
        sections = engine.create_sections("2025SP", sections_per_course={"OVR101": 4})
        result = engine.solve(sections, "Preflight Test", "2025SP")

        assert result.status in ("infeasible", "unknown")
        assert any("OVR101" in w for w in result.staffing_warnings)


def test_two_phase_forced_still_solves_correctly(db_manager):
    """two_phase=True: phase-1 placement + warm start, same correct outcome."""
    with db_manager.get_session() as session:
        _slots(session, 2)
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(_course("EZ101"))
        session.add(Faculty(id="F1", name="P", email="p@t.co", max_credits=12,
                            approved_courses=["EZ101"]))
        session.commit()

        engine = _engine(session)
        engine.config.solver_settings["two_phase"] = True
        engine.config.solver_settings["max_time_seconds"] = 60
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Two Phase Test", "2025SP")
        assert result.status in ("optimal", "feasible")
        assert result.stats.get("two_phase") is True


def test_two_phase_auto_skips_small_instances(db_manager):
    """auto mode leaves small models single-phase (threshold not reached)."""
    with db_manager.get_session() as session:
        _slots(session, 2)
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(_course("EZ101"))
        session.add(Faculty(id="F1", name="P", email="p@t.co", max_credits=12,
                            approved_courses=["EZ101"]))
        session.commit()

        engine = _engine(session)
        engine.config.solver_settings["max_time_seconds"] = 20
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "Auto Skip Test", "2025SP")
        assert result.status in ("optimal", "feasible")
        assert result.stats.get("two_phase") is False


def test_placement_hint_can_be_disabled(db_manager):
    """solver_settings.placement_hint=False still solves (flag respected)."""
    with db_manager.get_session() as session:
        _slots(session, 2)
        session.add(Room(id="R1", building="B", number="1", capacity=30,
                         room_type=RoomType.LECTURE))
        session.add(_course("EZ101"))
        session.add(Faculty(id="F1", name="P", email="p@t.co", max_credits=12,
                            approved_courses=["EZ101"]))
        session.commit()

        engine = _engine(session)
        engine.config.solver_settings["placement_hint"] = False
        engine.config.solver_settings["max_time_seconds"] = 20
        sections = engine.create_sections("2025SP")
        result = engine.solve(sections, "No Hint Test", "2025SP")
        assert result.status in ("optimal", "feasible")
