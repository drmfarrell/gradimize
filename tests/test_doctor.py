"""Data-health doctor: structured checks over imported data."""

import tempfile
from pathlib import Path

import pytest

from src.core.doctor import overall_status, run_health_checks
from src.core.ingest import DataIngestor
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Faculty,
    Prerequisite,
    Room,
    RoomType,
    StructureType,
    Student,
    StudentCourseHistory,
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


def _seed_clean(session):
    """Minimal dataset where every check should pass."""
    session.add(TimeSlot(id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                         start_time="09:00", end_time="09:50", duration_minutes=50,
                         slot_type="MWF_50"))
    session.add(Room(id="R1", building="B", number="1", capacity=30,
                     room_type=RoomType.LECTURE))
    session.add(Course(code="CSCI101", title="Intro", credits=3,
                       structure_type=StructureType.STANDALONE,
                       required_room_types=["LECTURE"], valid_slot_types=["MWF_50"]))
    session.add(Faculty(id="F1", name="Prof", email="p@t.co", max_credits=12,
                        approved_courses=["CSCI101"]))
    session.add(Student(id="S1", name="Student 1", email="s@t.co",
                        class_standing=ClassStanding.FR, credits_earned=10,
                        major="CSCI", catalog_year=2024))
    session.add(StudentDemand(student_id="S1", course_code="CSCI101",
                              urgency_score=10.0, is_required=True,
                              is_graduation_critical=False, reason="Test"))
    session.commit()


def _by_name(results):
    return {r.name: r for r in results}


def _seed_biennial_chain(session, *, biol401_required):
    """BIOL101 -> BIOL102 -> BIOL201 (annual, FALL-only) -> BIOL301
    (BIENNIAL_EVEN, spring-only) -> BIOL401 (BIENNIAL_ODD, spring-only), each
    a hard prerequisite of the next.

    Entering fall of an EVEN calendar year pushes BIOL401's earliest
    completion to term 10 (annual FALL-only courses complete terms 1/3/5,
    BIOL301 can't land before its even-year spring at term 8, BIOL401 can't
    land before its next odd-year spring at term 10) -- past the 8-term
    budget. Entering fall of an ODD year lands BIOL401 at exactly term 8
    (fine). The check must still flag it: students enter in both parities,
    and either one blowing the budget is enough.
    """
    codes = ["BIOL101", "BIOL102", "BIOL201", "BIOL301", "BIOL401"]
    cadences = ["ANNUAL", "ANNUAL", "ANNUAL", "BIENNIAL_EVEN", "BIENNIAL_ODD"]
    terms = [["FALL"], ["FALL"], ["FALL"], ["SPRING"], ["SPRING"]]
    for code, cadence, offer_terms in zip(codes, cadences, terms):
        session.add(Course(code=code, title=code, credits=3,
                           structure_type=StructureType.STANDALONE,
                           offer_terms=offer_terms, cadence=cadence))
    for course, prereq in zip(codes[1:], codes[:-1]):
        session.add(Prerequisite(course_code=course, prereq_code=prereq,
                                 is_corequisite=False))
    if biol401_required:
        session.add(DegreeRequirement(major="BIOL", course_code="BIOL401",
                                      is_required=True, catalog_year=2024))
    session.commit()


def test_clean_dataset_is_ok(db_manager):
    with db_manager.get_session() as session:
        _seed_clean(session)
        results = run_health_checks(session)
        by = _by_name(results)
        # environment check may warn (test DB lives in tmp, outside repo = ok)
        non_env = [r for r in results if r.name != "environment"]
        assert overall_status(non_env) == "ok", [
            (r.name, r.summary) for r in non_env if r.status != "ok"
        ]
        assert by["staffing-preflight"].status == "ok"


def test_orphan_history_student_fails(db_manager):
    with db_manager.get_session() as session:
        _seed_clean(session)
        session.add(StudentCourseHistory(student_id="GHOST", course_code="CSCI101",
                                         grade="B", semester="2024FA",
                                         credits_earned=3))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        assert by["history-orphan-students"].status == "fail"
        assert "GHOST" in by["history-orphan-students"].details[0]


def test_demanded_course_without_room_fails(db_manager):
    with db_manager.get_session() as session:
        _seed_clean(session)
        session.add(Course(code="CHEM999", title="No Lab", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LAB_CHEM"],
                           valid_slot_types=["MWF_50"]))
        session.add(StudentDemand(student_id="S1", course_code="CHEM999",
                                  urgency_score=10.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        assert by["course-no-compatible-room"].status == "fail"
        assert "CHEM999" in by["course-no-compatible-room"].details[0]


def test_broken_child_link_fails(db_manager):
    with db_manager.get_session() as session:
        _seed_clean(session)
        session.add(Course(code="BIOL101L", title="Lab", credits=1,
                           structure_type=StructureType.CHILD,
                           linked_course_code="BIOL101",  # doesn't exist
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"]))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        assert by["linked-course-integrity"].status == "fail"


def test_duplicate_demands_fail(db_manager):
    with db_manager.get_session() as session:
        _seed_clean(session)
        session.add(StudentDemand(student_id="S1", course_code="CSCI101",
                                  urgency_score=10.0, is_required=True,
                                  is_graduation_critical=False, reason="Dup"))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        assert by["duplicate-demand-rows"].status == "fail"


def test_unknown_grade_and_standing_mismatch_warn(db_manager):
    with db_manager.get_session() as session:
        _seed_clean(session)
        session.add(StudentCourseHistory(student_id="S1", course_code="CSCI101",
                                         grade="Z++", semester="2024FA",
                                         credits_earned=3))
        session.add(Student(id="S2", name="Student 2", email="s2@t.co",
                            class_standing=ClassStanding.SR, credits_earned=5,
                            major="CSCI", catalog_year=2024))
        session.add(StudentDemand(student_id="S2", course_code="CSCI101",
                                  urgency_score=10.0, is_required=True,
                                  is_graduation_critical=False, reason="Test"))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        assert by["unknown-grades"].status == "warn"
        assert "Z++" in by["unknown-grades"].details
        assert by["standing-credits-mismatch"].status == "warn"
        assert any("S2" in d for d in by["standing-credits-mismatch"].details)


def test_no_demands_warns(db_manager):
    with db_manager.get_session() as session:
        _seed_clean(session)
        from sqlalchemy import delete
        session.execute(delete(StudentDemand))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        assert by["no-demands"].status == "warn"


def test_overall_status_precedence():
    from src.core.doctor import CheckResult
    ok = CheckResult(name="a", status="ok", summary="")
    warn = CheckResult(name="b", status="warn", summary="")
    fail = CheckResult(name="c", status="fail", summary="")
    assert overall_status([ok]) == "ok"
    assert overall_status([ok, warn]) == "warn"
    assert overall_status([ok, warn, fail]) == "fail"


def test_contact_hours_low_minutes_warns(db_manager):
    """A 3-credit course whose only slot type delivers just 50 min/week
    (expected ~150, well under the 60% floor of 90) should be named."""
    with db_manager.get_session() as session:
        _seed_clean(session)
        # Single day, 50-minute slot: 50 min/week total, no other option.
        session.add(TimeSlot(id=2, name="M_0900", pattern="M", days=["M"],
                             start_time="09:00", end_time="09:50",
                             duration_minutes=50, slot_type="M_50"))
        session.add(Course(code="ARTS150", title="Studio Seminar", credits=3,
                           structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["M_50"]))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        result = by["contact-hours"]
        assert result.status == "warn"
        assert (
            "ARTS150: 3 credits but its slot options deliver 50-50 min/week "
            "(expected ~150)" in result.details
        )


def test_contact_hours_skips_child_labs_and_meeting_patterns(db_manager):
    """CHILD lab sections and multi-meeting (meeting_patterns) courses are
    exempt: separately-coded labs run long per credit by design, and
    meeting_patterns courses define their own per-meeting structure."""
    with db_manager.get_session() as session:
        _seed_clean(session)
        session.add(TimeSlot(id=3, name="M_0830_LAB", pattern="M", days=["M"],
                             start_time="08:30", end_time="11:00",
                             duration_minutes=150, slot_type="LAB_150"))
        session.add(Course(code="CSCI200", title="Intro Computing Lecture",
                           credits=3, structure_type=StructureType.PARENT,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["MWF_50"]))
        # 1-credit CHILD lab on a 150-min slot: 300% of the 50 min/week a
        # 1-credit course implies — would fail the tolerance band on its own,
        # but separately-coded labs are fine by design and skipped.
        session.add(Course(code="CSCI200L", title="Intro Computing Lab",
                           credits=1, structure_type=StructureType.CHILD,
                           linked_course_code="CSCI200",
                           required_room_types=["LAB_DRY"],
                           valid_slot_types=["LAB_150"]))
        # meeting_patterns set: skipped regardless of its (unused)
        # valid_slot_types/credits combination.
        session.add(Course(code="BMED330", title="Weird Meeting Course",
                           credits=3, structure_type=StructureType.STANDALONE,
                           required_room_types=["LECTURE"],
                           valid_slot_types=["LAB_150"],
                           meeting_patterns=[{"slot_types": ["LAB_150"],
                                              "room_types": ["LECTURE"]}]))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        result = by["contact-hours"]
        assert result.status == "ok", result.details
        assert not any("CSCI200L" in d for d in result.details)
        assert not any("BMED330" in d for d in result.details)


def test_contact_hours_clean_on_sample_catalog(db_manager):
    """Regression: the real sample catalog + time-slot grid must not trip
    the contact-hours check. Covers BIOL301 (4-credit COMBINED course whose
    MWF_50/TR_75 options deliver 150 min/week — 75% of the 200 expected,
    inside the 60% floor) and the CHILD labs (1 credit on LAB_150 = 150
    min/week, 300% of expected, but skipped as separately-coded labs)."""
    repo_root = Path(__file__).parent.parent
    with db_manager.get_session() as session:
        ingestor = DataIngestor(session, repo_root / "data" / "sample")
        courses = ingestor.load_courses()
        ingestor.load_time_slots(repo_root / "config" / "time_slots.yaml")
        assert not ingestor.errors, ingestor.errors

        by = _by_name(run_health_checks(session, deep=False))
        result = by["contact-hours"]
        assert result.status == "ok", result.details

        expected_checked = sum(
            1 for c in courses
            if c.structure_type != StructureType.CHILD and not c.meeting_patterns
        )
        assert str(expected_checked) in result.summary


def test_api_health_endpoint(db_manager):
    """GET /data/health returns the graded report."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes.data import get_session, router

    with db_manager.get_session() as session:
        _seed_clean(session)

    app = FastAPI()
    app.include_router(router)

    def override_session():
        with db_manager.get_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)
    resp = client.get("/data/health?deep=false")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "warn", "fail")
    names = {r["name"] for r in body["results"]}
    assert "history-orphan-students" in names
    assert "environment" in names


def test_annual_chain_completes_within_budget_is_ok(db_manager):
    """A plain 4-course ANNUAL chain (any-term offer_terms, so available
    every term) completes at terms 1/2/3/4 -- nowhere near the 8-term
    budget, on either entry parity."""
    with db_manager.get_session() as session:
        for code in ("CHEM101", "CHEM201", "CHEM301", "CHEM401"):
            session.add(Course(code=code, title=code, credits=3,
                               structure_type=StructureType.STANDALONE))
        for course, prereq in zip(
            ("CHEM201", "CHEM301", "CHEM401"),
            ("CHEM101", "CHEM201", "CHEM301"),
        ):
            session.add(Prerequisite(course_code=course, prereq_code=prereq,
                                     is_corequisite=False))
        session.add(DegreeRequirement(major="CHEM", course_code="CHEM401",
                                      is_required=True, catalog_year=2024))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        result = by["prereq-chain-vs-cadence"]
        assert result.status == "ok", result.details
        assert result.details == []


def test_biennial_chain_over_budget_required_fails(db_manager):
    """The BIOL101->...->BIOL401 chain needs term 10 entering an even-year
    fall (only term 8 entering an odd-year fall) -- flagged because EITHER
    parity blowing the 8-term budget is enough. BIOL401 is a required
    course, so an unreachable chain is a graduation blocker: fail."""
    with db_manager.get_session() as session:
        _seed_biennial_chain(session, biol401_required=True)
        by = _by_name(run_health_checks(session, deep=False))
        result = by["prereq-chain-vs-cadence"]
        assert result.status == "fail"
        assert any("BIOL401" in d for d in result.details)
        # The detail should reconstruct the binding chain and call out
        # which links on it are biennial.
        detail = next(d for d in result.details if "BIOL401" in d)
        assert "BIOL101" in detail and "BIOL301" in detail
        assert "biennial links" in detail
        assert "BIOL301" in detail.split("biennial links")[1]
        assert "BIOL401" in detail.split("biennial links")[1]


def test_biennial_chain_over_budget_elective_warns(db_manager):
    """Same unreachable chain, but BIOL401 isn't anyone's degree
    requirement -- an elective delay, not a graduation blocker: warn."""
    with db_manager.get_session() as session:
        _seed_biennial_chain(session, biol401_required=False)
        by = _by_name(run_health_checks(session, deep=False))
        result = by["prereq-chain-vs-cadence"]
        assert result.status == "warn"
        assert any("BIOL401" in d for d in result.details)


def test_corequisite_does_not_add_a_term(db_manager):
    """A corequisite may be taken the SAME term (constraint is <=, not the
    strictly-before < that hard prerequisites use). MATH_A is FALL-only
    with corequisite MATH_B (any term); MATH_C hard-requires MATH_A. MATH_A
    completes term 1 (no delay from its coreq) so MATH_C completes term 2 --
    a bug that treated the coreq like a hard prereq (+1 term) would instead
    produce term 2 for MATH_A and term 3 for MATH_C."""
    with db_manager.get_session() as session:
        session.add(Course(code="MATH_A", title="A", credits=3,
                           structure_type=StructureType.STANDALONE,
                           offer_terms=["FALL"]))
        session.add(Course(code="MATH_B", title="B", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Course(code="MATH_C", title="C", credits=3,
                           structure_type=StructureType.STANDALONE))
        session.add(Prerequisite(course_code="MATH_A", prereq_code="MATH_B",
                                 is_corequisite=True))
        session.add(Prerequisite(course_code="MATH_C", prereq_code="MATH_A",
                                 is_corequisite=False))
        session.commit()

        by = _by_name(run_health_checks(session, deep=False))
        assert by["prereq-chain-vs-cadence"].status == "ok"

        from sqlmodel import select

        from src.core.doctor import _prereq_earliest_completions

        courses = {c.code: c for c in session.exec(select(Course)).all()}
        prereqs_by_course = {}
        for p in session.exec(select(Prerequisite)).all():
            prereqs_by_course.setdefault(p.course_code, []).append(
                (p.prereq_code, p.is_corequisite)
            )
        earliest, _, _ = _prereq_earliest_completions(courses, prereqs_by_course, 2026)
        assert earliest["MATH_A"] == 1
        assert earliest["MATH_C"] == 2


def test_summer_only_course_never_offered_on_regular_horizon(db_manager):
    """offer_terms=["SUMMER"] with no FALL/SPRING option is never available
    on the regular-term horizon (summer is deliberately excluded as a
    planning term) -- distinct wording from a chain that's merely slow."""
    with db_manager.get_session() as session:
        session.add(Course(code="XYZ999", title="Summer Only", credits=3,
                           structure_type=StructureType.STANDALONE,
                           offer_terms=["SUMMER"]))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        result = by["prereq-chain-vs-cadence"]
        assert result.status == "warn"
        assert any(
            "XYZ999" in d and "never offered" in d for d in result.details
        )


def test_degree_requirement_coverage_flags_uncovered_catalog_years(db_manager):
    """Continuing students under a catalog year with zero DegreeRequirement
    rows silently lose required/grad-critical weighting (exact-match lookup,
    no fallback) -- the doctor must flag the uncovered (major, year) pairs.
    With NO requirements transcribed at all the check stays silent (partial
    coverage is the dangerous state; total absence is obvious everywhere)."""
    with db_manager.get_session() as session:
        _seed_clean(session)
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        assert "degree-requirement-coverage" not in by  # zero reqs -> skipped

        # Transcribe requirements for CSCI/2024 (covers S1) but not BIOL/2022.
        session.add(DegreeRequirement(major="CSCI", course_code="CSCI101",
                                      is_required=True, category="core",
                                      catalog_year=2024))
        session.add(Student(id="S9", name="Old Catalog", email="s9@t.co",
                            class_standing=ClassStanding.SR, credits_earned=100,
                            major="BIOL", catalog_year=2022))
        session.commit()
        by = _by_name(run_health_checks(session, deep=False))
        result = by["degree-requirement-coverage"]
        assert result.status == "warn"
        assert any("BIOL/2022" in d for d in result.details)
        assert not any("CSCI/2024" in d for d in result.details)
