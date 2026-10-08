"""Tests for the one-student replanner."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.replan import render_replan_markdown, replan_student
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Prerequisite,
    StructureType,
    Student,
    StudentCourseHistory,
)

CATALOG_YEAR = 2024


@pytest.fixture
def source_db():
    """A seeded source database FILE (replan_student copies it)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    manager = DatabaseManager(db_path)
    manager.create_tables()
    yield db_path, manager
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def _seed_catalog(session):
    """MATH101 -> MATH102 -> MATH201 chain (ANNUAL, any-term), plus a
    BIENNIAL_EVEN/SPRING-only BIOL301. All four required for major "TEST".
    """
    courses = [
        Course(code="MATH101", title="College Algebra", credits=3,
               structure_type=StructureType.STANDALONE),
        Course(code="MATH102", title="Calculus I", credits=3,
               structure_type=StructureType.STANDALONE),
        Course(code="MATH201", title="Calculus II", credits=3,
               structure_type=StructureType.STANDALONE),
        Course(code="BIOL301", title="Genetics", credits=3,
               structure_type=StructureType.STANDALONE,
               cadence="BIENNIAL_EVEN", offer_terms=["SPRING"]),
    ]
    for c in courses:
        session.add(c)
    session.add(Prerequisite(course_code="MATH102", prereq_code="MATH101", min_grade="D"))
    session.add(Prerequisite(course_code="MATH201", prereq_code="MATH102", min_grade="D"))
    for code in ("MATH101", "MATH102", "MATH201", "BIOL301"):
        session.add(DegreeRequirement(major="TEST", course_code=code,
                                       is_required=True, category="core",
                                       catalog_year=CATALOG_YEAR))
    session.commit()


def _add_student(session, student_id="S1", *, credits_earned=0,
                  class_standing=ClassStanding.FR):
    session.add(Student(id=student_id, name="Student One", email="s1@test.edu",
                         class_standing=class_standing, credits_earned=credits_earned,
                         major="TEST", catalog_year=CATALOG_YEAR))
    session.commit()


def _history(session, student_id, course_code, grade, semester, credits_earned):
    session.add(StudentCourseHistory(student_id=student_id, course_code=course_code,
                                      grade=grade, semester=semester,
                                      credits_earned=credits_earned))


def _term_index_of(plan: dict, code: str) -> int | None:
    """Index of the first term in the plan whose courses include `code`."""
    for i, t in enumerate(plan["terms"]):
        if any(c["code"] == code for c in t["courses"]):
            return i
    return None


# ─────────────────────────────────────────────────────────────────
# 1. Freshman, empty history: prereq chain in order, credit cap respected,
#    graduation detected once all four courses are simulated.
# ─────────────────────────────────────────────────────────────────


def test_freshman_prereq_chain_in_order(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)
        _add_student(session)

    plan = replan_student(db_path, "S1", start_term="2026FA", credits_per_term=3)

    assert plan["student_id"] == "S1"
    assert plan["student_name"] == "Student One"
    assert plan["major"] == "TEST"
    assert plan["start_term"] == "2026FA"
    assert plan["starting_credits"] == 0
    assert plan["already_complete"] is False

    # Every term stays within the credit cap.
    for t in plan["terms"]:
        assert t["credits"] <= 3
        assert sum(c["credits"] for c in t["courses"]) == t["credits"]

    # Prereq ordering: MATH102 strictly after MATH101, MATH201 strictly
    # after MATH102 (the graph makes this impossible to violate, but check
    # the plan reflects it).
    i101 = _term_index_of(plan, "MATH101")
    i102 = _term_index_of(plan, "MATH102")
    i201 = _term_index_of(plan, "MATH201")
    assert i101 is not None and i102 is not None and i201 is not None
    assert i101 < i102 < i201
    # MATH102 must land at least one simulated term after MATH101's term.
    assert plan["terms"][i102]["term"] != plan["terms"][i101]["term"]

    assert plan["terms"][0]["term"] == "2026FA"
    assert plan["graduates_after"] == plan["terms"][-1]["term"]
    assert plan["projected_credits"] == plan["starting_credits"] + sum(
        t["credits"] for t in plan["terms"]
    )
    assert plan["remaining"] == []

    md = render_replan_markdown(plan)
    assert "2026FA" in md
    assert "MATH101" in md
    assert f"complete after {plan['graduates_after']}" in md
    assert "Capacity not simulated" in md


# ─────────────────────────────────────────────────────────────────
# 2. Failed MATH102 -> retaken before MATH201 (retake-first ordering).
# ─────────────────────────────────────────────────────────────────


def test_failed_course_is_retaken_before_dependent(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)
        _add_student(session, credits_earned=3)
        _history(session, "S1", "MATH101", "B", "2025FA", 3)
        _history(session, "S1", "MATH102", "F", "2026SP", 0)
        session.commit()

    plan = replan_student(db_path, "S1", start_term="2026FA", credits_per_term=3)

    i102 = _term_index_of(plan, "MATH102")
    i201 = _term_index_of(plan, "MATH201")
    assert i102 is not None, "the failed course must reappear in the plan"
    assert i201 is not None
    assert i102 < i201, "retake MATH102 before advancing to MATH201"
    # MATH101 must NOT reappear — it was already passed.
    assert _term_index_of(plan, "MATH101") is None


# ─────────────────────────────────────────────────────────────────
# 3. Biennial gap: BIOL301 is the only outstanding requirement, and the
#    start term is chosen so the next even-year spring is >= 2 regular
#    terms away.
# ─────────────────────────────────────────────────────────────────


def test_biennial_gap_warns_and_lands_on_even_spring(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)
        _add_student(session, credits_earned=9, class_standing=ClassStanding.SO)
        _history(session, "S1", "MATH101", "B", "2025FA", 3)
        _history(session, "S1", "MATH102", "B", "2026SP", 3)
        _history(session, "S1", "MATH201", "B", "2026FA", 3)
        session.commit()

    # 2027SP (odd year) -> 2027FA -> 2028SP (even year): 2 regular terms away.
    plan = replan_student(db_path, "S1", start_term="2027SP")

    assert len(plan["warnings"]) == 1, plan["warnings"]
    assert "BIOL301" in plan["warnings"][0]
    assert "2028SP" in plan["warnings"][0]

    i301 = _term_index_of(plan, "BIOL301")
    assert i301 is not None
    assert plan["terms"][i301]["term"] == "2028SP"
    assert plan["graduates_after"] == "2028SP"


# ─────────────────────────────────────────────────────────────────
# 4. Everything already passed: already_complete, no terms simulated.
# ─────────────────────────────────────────────────────────────────


def test_already_complete_student(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)
        _add_student(session, credits_earned=12, class_standing=ClassStanding.SO)
        _history(session, "S1", "MATH101", "B", "2025FA", 3)
        _history(session, "S1", "MATH102", "B", "2026SP", 3)
        _history(session, "S1", "MATH201", "B", "2026FA", 3)
        _history(session, "S1", "BIOL301", "B", "2026SP", 3)
        session.commit()

    plan = replan_student(db_path, "S1", start_term="2026FA")

    assert plan["already_complete"] is True
    assert plan["terms"] == []
    assert plan["graduates_after"] is None
    assert plan["remaining"] == []
    assert plan["projected_credits"] == plan["starting_credits"] == 12

    md = render_replan_markdown(plan)
    assert "already satisfied" in md
    assert "Capacity not simulated" in md


# ─────────────────────────────────────────────────────────────────
# 5. max_terms exhaustion: only one term simulated out of a three-course
#    chain -> "remaining" lists what's left, with next-offered terms.
# ─────────────────────────────────────────────────────────────────


def test_max_terms_exhaustion_reports_remaining(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)
        _add_student(session)

    plan = replan_student(db_path, "S1", start_term="2026FA", max_terms=1)

    assert plan["already_complete"] is False
    assert plan["graduates_after"] is None
    assert len(plan["terms"]) == 1
    assert plan["terms"][0]["term"] == "2026FA"
    assert [c["code"] for c in plan["terms"][0]["courses"]] == ["MATH101"]

    remaining_by_code = {r["code"]: r["next_offered"] for r in plan["remaining"]}
    # MATH102 becomes available immediately (ANNUAL/any-term) the very next
    # regular term; BIOL301 has to wait for the next even-year spring.
    assert remaining_by_code == {"MATH102": "2027SP", "BIOL301": "2028SP"}
    # MATH201 is still prereq-gated behind MATH102 (not yet visible to the
    # demand calculator), so it correctly does NOT show up as "remaining"
    # yet — it isn't "needed" in the calculator's eyes until MATH102 lands.
    assert "MATH201" not in remaining_by_code

    md = render_replan_markdown(plan)
    assert "still outstanding" in md
    assert "MATH102" in md and "2027SP" in md


# ─────────────────────────────────────────────────────────────────
# 6. Source-DB safety: the source database is never written to.
# ─────────────────────────────────────────────────────────────────


def test_source_database_untouched(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)
        _add_student(session, credits_earned=3)
        _history(session, "S1", "MATH101", "B", "2025FA", 3)
        session.commit()

    def _history_count():
        with manager.get_session() as session:
            return len(session.exec(
                select(StudentCourseHistory).where(StudentCourseHistory.student_id == "S1")
            ).all())

    before = _history_count()
    assert before == 1

    replan_student(db_path, "S1", start_term="2026FA", max_terms=6, credits_per_term=3)

    after = _history_count()
    assert after == before == 1


# ─────────────────────────────────────────────────────────────────
# Misc edge cases
# ─────────────────────────────────────────────────────────────────


def test_unknown_student_raises_value_error(source_db):
    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)

    with pytest.raises(ValueError):
        replan_student(db_path, "NOSUCHSTUDENT")


def test_missing_source_db_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        replan_student(Path("/nonexistent/path/does-not-exist.db"), "S1")


# ─────────────────────────────────────────────────────────────────
# API route (thin wrapper in src/api/routes/data.py)
# ─────────────────────────────────────────────────────────────────


def test_replan_route_smoke(source_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes import data as data_mod

    db_path, manager = source_db
    with manager.get_session() as session:
        _seed_catalog(session)
        _add_student(session, "S1")

    monkeypatch.setattr(data_mod, "REPLAN_DB_PATH", db_path)
    app = FastAPI()
    app.include_router(data_mod.router)
    client = TestClient(app)

    resp = client.get("/data/students/S1/replan?start=2026FA")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "MATH101" in resp.text

    as_json = client.get("/data/students/S1/replan?start=2026FA&as_markdown=false")
    assert as_json.status_code == 200
    assert as_json.json()["student_id"] == "S1"

    assert client.get(
        "/data/students/NOPE/replan?start=2026FA"
    ).status_code == 404
