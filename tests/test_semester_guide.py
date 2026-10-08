"""Tests for semester guide feature."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.demand import DemandCalculator, estimate_student_semester
from src.core.graph import PrerequisiteGraph
from src.core.ingest import DataIngestor
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Prerequisite,
    SemesterGuide,
    SemesterGuideEntry,
    StructureType,
    Student,
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
def seeded_session(db_manager):
    """Seed database with courses, prereqs, requirements, and students."""
    with db_manager.get_session() as session:
        # Courses
        courses = [
            Course(code="BIOL101", title="Intro Biology", credits=4,
                   structure_type=StructureType.STANDALONE),
            Course(code="BIOL201", title="Cell Biology", credits=4,
                   structure_type=StructureType.STANDALONE),
            Course(code="BIOL301", title="Genetics", credits=3,
                   structure_type=StructureType.STANDALONE),
            Course(code="CHEM101", title="General Chemistry I", credits=4,
                   structure_type=StructureType.STANDALONE),
            Course(code="MATH101", title="Calculus I", credits=3,
                   structure_type=StructureType.STANDALONE),
            Course(code="ENGL101", title="English Comp I", credits=3,
                   structure_type=StructureType.STANDALONE),
            Course(code="HIST101", title="World History", credits=3,
                   structure_type=StructureType.STANDALONE),
            Course(code="CSCI101", title="Intro to CS", credits=3,
                   structure_type=StructureType.STANDALONE),
            Course(code="CSCI201", title="Data Structures", credits=3,
                   structure_type=StructureType.STANDALONE),
            Course(code="CHEM301", title="Organic Chemistry I", credits=3,
                   structure_type=StructureType.STANDALONE),
        ]
        for c in courses:
            session.add(c)

        # Prerequisites
        session.add(Prerequisite(course_code="BIOL201", prereq_code="BIOL101", min_grade="C"))
        session.add(Prerequisite(course_code="BIOL301", prereq_code="BIOL201", min_grade="C"))
        session.add(Prerequisite(course_code="CSCI201", prereq_code="CSCI101", min_grade="C"))
        session.add(Prerequisite(course_code="CHEM301", prereq_code="CHEM101", min_grade="C"))

        # Degree requirements for BIOL (catalog_year=2024)
        session.add(DegreeRequirement(major="BIOL", course_code="BIOL101",
                                      is_required=True, category="core", catalog_year=2024))
        session.add(DegreeRequirement(major="BIOL", course_code="BIOL201",
                                      is_required=True, category="core", catalog_year=2024))
        session.add(DegreeRequirement(major="BIOL", course_code="BIOL301",
                                      is_required=True, category="core", catalog_year=2024))
        session.add(DegreeRequirement(major="BIOL", course_code="CHEM101",
                                      is_required=True, category="core", catalog_year=2024))
        session.add(DegreeRequirement(major="BIOL", course_code="MATH101",
                                      is_required=True, category="core", catalog_year=2024))
        session.add(DegreeRequirement(major="BIOL", course_code="ENGL101",
                                      is_required=True, category="general_ed", catalog_year=2024))
        session.add(DegreeRequirement(major="BIOL", course_code="HIST101",
                                      is_required=False, category="general_ed", catalog_year=2024))

        # Degree requirements for CSCI (catalog_year=2024)
        session.add(DegreeRequirement(major="CSCI", course_code="CSCI101",
                                      is_required=True, category="core", catalog_year=2024))
        session.add(DegreeRequirement(major="CSCI", course_code="CSCI201",
                                      is_required=True, category="core", catalog_year=2024))
        session.add(DegreeRequirement(major="CSCI", course_code="MATH101",
                                      is_required=True, category="core", catalog_year=2024))

        # Concentration-specific requirement for BIOL/Pre-Medicine
        session.add(DegreeRequirement(major="BIOL", concentration="Pre-Medicine",
                                      course_code="CHEM301", is_required=True,
                                      category="concentration", catalog_year=2024))

        session.commit()
        yield session


def _make_guide_with_entries(session, major, catalog_year, entries,
                             concentration=None, name=None):
    """Helper to create a guide with entries.

    entries: list of (semester_number, course_code, category, credits, label)
    """
    if name is None:
        name = f"{major} {catalog_year} Plan"
    guide = SemesterGuide(
        major=major, concentration=concentration,
        catalog_year=catalog_year, name=name,
    )
    session.add(guide)
    session.commit()
    session.refresh(guide)

    for sem_num, course_code, category, credits, label in entries:
        entry = SemesterGuideEntry(
            guide_id=guide.id,
            semester_number=sem_num,
            course_code=course_code,
            category=category,
            credits=credits,
            label=label,
        )
        session.add(entry)
    session.commit()
    return guide


# ─────────────────────────────────────────────────────────────────
# 1. estimate_student_semester
# ─────────────────────────────────────────────────────────────────


def test_estimate_student_semester():
    """Credits-to-semester mapping works correctly."""
    assert estimate_student_semester(0) == 1
    assert estimate_student_semester(14) == 1
    assert estimate_student_semester(15) == 2
    assert estimate_student_semester(29) == 2
    assert estimate_student_semester(30) == 3
    assert estimate_student_semester(120) == 8
    assert estimate_student_semester(200) == 8  # capped at 8


# ─────────────────────────────────────────────────────────────────
# 2. No guide = no change
# ─────────────────────────────────────────────────────────────────


def test_no_guide_no_change(seeded_session):
    """Urgency is identical when no semester guide exists."""
    session = seeded_session

    # Create a freshman BIOL student
    session.add(Student(
        id="S_FRESH", name="Fresh Bio", email="fb@test.com",
        class_standing=ClassStanding.FR, credits_earned=0,
        major="BIOL", catalog_year=2024,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = DemandCalculator(session, graph)
    student = session.get(Student, "S_FRESH")

    demands = calc.calculate_student_demand(student)
    assert len(demands) > 0

    # Verify no guide-related reasons in any demand
    for d in demands:
        assert "Recommended semester" not in d.reason
        assert "Fills" not in d.reason
        assert "Upcoming" not in d.reason


# ─────────────────────────────────────────────────────────────────
# 3. On-track specific course boost (1.3x)
# ─────────────────────────────────────────────────────────────────


def test_on_track_specific_course_boost(seeded_session):
    """Matching course in current semester gets 1.3x boost."""
    session = seeded_session

    # Freshman (0 credits => semester 1)
    session.add(Student(
        id="S_ON", name="On Track", email="on@test.com",
        class_standing=ClassStanding.FR, credits_earned=0,
        major="BIOL", catalog_year=2024,
    ))
    session.commit()

    # First get baseline without guide
    graph = PrerequisiteGraph(session)
    calc_no_guide = DemandCalculator(session, graph)
    student = session.get(Student, "S_ON")
    demands_no_guide = calc_no_guide.calculate_student_demand(student)
    baseline = {d.course_code: d.urgency_score for d in demands_no_guide}

    # Now add a guide with BIOL101 in semester 1
    _make_guide_with_entries(session, "BIOL", 2024, [
        (1, "BIOL101", None, 4, "Introduction to Biology"),
    ])

    # Recalculate with guide
    calc_guide = DemandCalculator(session, graph)
    demands_guide = calc_guide.calculate_student_demand(student)
    boosted = {d.course_code: d.urgency_score for d in demands_guide}

    # BIOL101 should be boosted by 1.3x
    assert "BIOL101" in baseline
    assert "BIOL101" in boosted
    assert abs(boosted["BIOL101"] - baseline["BIOL101"] * 1.3) < 0.01

    # Check reason includes semester reference
    biol_demand = next(d for d in demands_guide if d.course_code == "BIOL101")
    assert "Recommended semester 1" in biol_demand.reason


# ─────────────────────────────────────────────────────────────────
# 4. Behind schedule catch-up boost (1.4x)
# ─────────────────────────────────────────────────────────────────


def test_behind_schedule_catch_up_boost(seeded_session):
    """Earlier semester course not taken gets 1.4x boost."""
    session = seeded_session

    # Sophomore (15 credits => semester 2) who hasn't taken BIOL101
    session.add(Student(
        id="S_BEHIND", name="Behind", email="behind@test.com",
        class_standing=ClassStanding.SO, credits_earned=15,
        major="BIOL", catalog_year=2024,
    ))
    session.commit()

    # Get baseline without guide
    graph = PrerequisiteGraph(session)
    calc_no_guide = DemandCalculator(session, graph)
    student = session.get(Student, "S_BEHIND")
    demands_no_guide = calc_no_guide.calculate_student_demand(student)
    baseline = {d.course_code: d.urgency_score for d in demands_no_guide}

    # Guide says BIOL101 should be in semester 1, student is in semester 2
    _make_guide_with_entries(session, "BIOL", 2024, [
        (1, "BIOL101", None, 4, "Introduction to Biology"),
    ])

    calc_guide = DemandCalculator(session, graph)
    demands_guide = calc_guide.calculate_student_demand(student)
    boosted = {d.course_code: d.urgency_score for d in demands_guide}

    assert "BIOL101" in baseline
    assert "BIOL101" in boosted
    assert abs(boosted["BIOL101"] - baseline["BIOL101"] * 1.4) < 0.01


# ─────────────────────────────────────────────────────────────────
# 5. Generic category match
# ─────────────────────────────────────────────────────────────────


def test_generic_category_match(seeded_session):
    """Generic 'general_ed' slot boosts matching elective."""
    session = seeded_session

    # Freshman BIOL student
    session.add(Student(
        id="S_GEN", name="Gen Ed", email="gen@test.com",
        class_standing=ClassStanding.FR, credits_earned=0,
        major="BIOL", catalog_year=2024,
    ))
    session.commit()

    # Baseline without guide
    graph = PrerequisiteGraph(session)
    calc_no_guide = DemandCalculator(session, graph)
    student = session.get(Student, "S_GEN")
    demands_no_guide = calc_no_guide.calculate_student_demand(student)
    baseline = {d.course_code: d.urgency_score for d in demands_no_guide}

    # Guide has a generic general_ed slot in semester 1
    _make_guide_with_entries(session, "BIOL", 2024, [
        (1, None, "general_ed", 3, "Humanities Elective"),
    ])

    calc_guide = DemandCalculator(session, graph)
    demands_guide = calc_guide.calculate_student_demand(student)
    boosted = {d.course_code: d.urgency_score for d in demands_guide}

    # ENGL101 and HIST101 are general_ed courses, should get 1.15x on-track
    # (ENGL101 is required general_ed, HIST101 is elective general_ed)
    for code in ["ENGL101", "HIST101"]:
        if code in baseline and code in boosted:
            assert abs(boosted[code] - baseline[code] * 1.15) < 0.01, (
                f"{code}: expected {baseline[code] * 1.15}, got {boosted[code]}"
            )

    # Check reason
    engl_demand = next((d for d in demands_guide if d.course_code == "ENGL101"), None)
    if engl_demand:
        assert "Fills Humanities Elective slot" in engl_demand.reason


# ─────────────────────────────────────────────────────────────────
# 6. Next semester lookahead (1.1x)
# ─────────────────────────────────────────────────────────────────


def test_next_semester_lookahead(seeded_session):
    """Next semester's course gets 1.1x boost."""
    session = seeded_session

    # Freshman (0 credits => semester 1)
    session.add(Student(
        id="S_LOOK", name="Lookahead", email="look@test.com",
        class_standing=ClassStanding.FR, credits_earned=0,
        major="BIOL", catalog_year=2024,
    ))
    session.commit()

    # Baseline
    graph = PrerequisiteGraph(session)
    calc_no_guide = DemandCalculator(session, graph)
    student = session.get(Student, "S_LOOK")
    demands_no_guide = calc_no_guide.calculate_student_demand(student)
    baseline = {d.course_code: d.urgency_score for d in demands_no_guide}

    # Guide: CHEM101 is in semester 2 (student is in semester 1 => lookahead)
    _make_guide_with_entries(session, "BIOL", 2024, [
        (2, "CHEM101", None, 4, "General Chemistry I"),
    ])

    calc_guide = DemandCalculator(session, graph)
    demands_guide = calc_guide.calculate_student_demand(student)
    boosted = {d.course_code: d.urgency_score for d in demands_guide}

    assert "CHEM101" in baseline
    assert "CHEM101" in boosted
    assert abs(boosted["CHEM101"] - baseline["CHEM101"] * 1.1) < 0.01

    chem_demand = next(d for d in demands_guide if d.course_code == "CHEM101")
    assert "Upcoming (semester 2)" in chem_demand.reason


# ─────────────────────────────────────────────────────────────────
# 7. CSV ingestion creates guide
# ─────────────────────────────────────────────────────────────────


def test_csv_ingestion_creates_guide(seeded_session, tmp_path):
    """CSV loads correctly into both SemesterGuide and SemesterGuideEntry tables."""
    session = seeded_session

    csv_file = tmp_path / "semester_guide.csv"
    csv_file.write_text(
        "major,concentration,catalog_year,semester_number,course_code,category,credits,label\n"
        "BIOL,,2024,1,BIOL101,,4,Introduction to Biology\n"
        "BIOL,,2024,1,CHEM101,,4,General Chemistry I\n"
        "BIOL,,2024,1,,general_ed,3,Humanities Elective\n"
        "BIOL,,2024,2,BIOL201,,4,Cell Biology\n"
    )

    ingestor = DataIngestor(session, tmp_path)
    guides = ingestor.load_semester_guides()

    assert len(guides) == 1
    guide = guides[0]
    assert guide.major == "BIOL"
    assert guide.catalog_year == 2024
    assert guide.concentration is None

    entries = session.exec(
        select(SemesterGuideEntry).where(SemesterGuideEntry.guide_id == guide.id)
    ).all()
    assert len(entries) == 4

    sem1_entries = [e for e in entries if e.semester_number == 1]
    assert len(sem1_entries) == 3

    generic = [e for e in entries if e.course_code is None]
    assert len(generic) == 1
    assert generic[0].category == "general_ed"


# ─────────────────────────────────────────────────────────────────
# 8. CSV validates course codes
# ─────────────────────────────────────────────────────────────────


def test_csv_validates_course_codes(seeded_session, tmp_path):
    """Invalid course_code is rejected during ingestion."""
    session = seeded_session

    csv_file = tmp_path / "semester_guide.csv"
    csv_file.write_text(
        "major,concentration,catalog_year,semester_number,course_code,category,credits,label\n"
        "BIOL,,2024,1,BIOL101,,4,Introduction to Biology\n"
        "BIOL,,2024,1,FAKE999,,3,Fake Course\n"
    )

    ingestor = DataIngestor(session, tmp_path)
    guides = ingestor.load_semester_guides()

    # Guide should still be created but only valid entry included
    assert len(guides) == 1
    entries = session.exec(
        select(SemesterGuideEntry).where(SemesterGuideEntry.guide_id == guides[0].id)
    ).all()
    assert len(entries) == 1
    assert entries[0].course_code == "BIOL101"

    # Error should be recorded
    assert any("FAKE999" in e for e in ingestor.errors)


# ─────────────────────────────────────────────────────────────────
# 9. Concentration-specific guide
# ─────────────────────────────────────────────────────────────────


def test_concentration_specific_guide(seeded_session):
    """Concentration-specific entries only apply to matching students."""
    session = seeded_session

    # Two students: one with Pre-Medicine concentration, one without
    session.add(Student(
        id="S_PREMED", name="PreMed", email="pm@test.com",
        class_standing=ClassStanding.FR, credits_earned=0,
        major="BIOL", concentration="Pre-Medicine", catalog_year=2024,
    ))
    session.add(Student(
        id="S_GENBIO", name="GenBio", email="gb@test.com",
        class_standing=ClassStanding.FR, credits_earned=0,
        major="BIOL", catalog_year=2024,
    ))
    session.commit()

    # Create a base guide and a Pre-Medicine guide
    _make_guide_with_entries(session, "BIOL", 2024, [
        (1, "BIOL101", None, 4, "Introduction to Biology"),
    ], name="BIOL Base Plan")

    _make_guide_with_entries(session, "BIOL", 2024, [
        (1, "BIOL101", None, 4, "Introduction to Biology"),
        (1, "CHEM101", None, 4, "General Chemistry I"),
    ], concentration="Pre-Medicine", name="BIOL Pre-Med Plan")

    graph = PrerequisiteGraph(session)

    # Pre-Med student should match the concentration-specific guide
    calc = DemandCalculator(session, graph)
    premed = session.get(Student, "S_PREMED")
    premed_demands = calc.calculate_student_demand(premed)
    premed_map = {d.course_code: d for d in premed_demands}

    # CHEM101 should be boosted for Pre-Med (it's in the conc guide semester 1)
    assert "CHEM101" in premed_map
    assert "Recommended semester 1" in premed_map["CHEM101"].reason

    # General BIOL student should use the base guide
    calc2 = DemandCalculator(session, graph)
    genbio = session.get(Student, "S_GENBIO")
    genbio_demands = calc2.calculate_student_demand(genbio)
    genbio_map = {d.course_code: d for d in genbio_demands}

    # CHEM101 should NOT have guide boost for general bio (not in base guide)
    if "CHEM101" in genbio_map:
        assert "Recommended semester" not in genbio_map["CHEM101"].reason


# ─────────────────────────────────────────────────────────────────
# 10. Guide doesn't affect unrelated major
# ─────────────────────────────────────────────────────────────────


def test_guide_does_not_affect_unrelated_major(seeded_session):
    """BIOL guide doesn't affect CSCI students."""
    session = seeded_session

    # CSCI student
    session.add(Student(
        id="S_CSCI", name="CS Student", email="cs@test.com",
        class_standing=ClassStanding.FR, credits_earned=0,
        major="CSCI", catalog_year=2024,
    ))
    session.commit()

    # Baseline without any guide
    graph = PrerequisiteGraph(session)
    calc_no_guide = DemandCalculator(session, graph)
    student = session.get(Student, "S_CSCI")
    demands_no_guide = calc_no_guide.calculate_student_demand(student)
    baseline = {d.course_code: d.urgency_score for d in demands_no_guide}

    # Add a BIOL guide
    _make_guide_with_entries(session, "BIOL", 2024, [
        (1, "BIOL101", None, 4, "Introduction to Biology"),
        (1, "MATH101", None, 3, "Calculus I"),
    ])

    # CSCI student demand should be unchanged
    calc_guide = DemandCalculator(session, graph)
    demands_guide = calc_guide.calculate_student_demand(student)
    with_guide = {d.course_code: d.urgency_score for d in demands_guide}

    for code in baseline:
        assert abs(with_guide.get(code, 0) - baseline[code]) < 0.01, (
            f"{code}: expected {baseline[code]}, got {with_guide.get(code, 0)}"
        )
