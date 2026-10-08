"""Tests for freshmen enrollment projection feature."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.demand import DemandCalculator, ProjectionDemandCalculator
from src.core.graph import PrerequisiteGraph
from src.core.ingest import DataIngestor
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    FreshmenProjection,
    Prerequisite,
    Student,
    StudentDemand,
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
    """Seed database with minimal courses, prereqs, and degree requirements."""
    with db_manager.get_session() as session:
        # Courses
        session.add(Course(
            code="CSCI101", title="Intro to CS", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))
        session.add(Course(
            code="CSCI201", title="Data Structures", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))
        session.add(Course(
            code="ENGL101", title="English Comp I", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))
        session.add(Course(
            code="MATH101", title="College Algebra", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))
        session.add(Course(
            code="BIOL101", title="Intro Biology", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))
        session.add(Course(
            code="BIOL201", title="Cell Biology", credits=3,
            structure_type="STANDALONE", required_room_types=["LECTURE"],
            valid_slot_types=["MWF_50"],
        ))

        # Prerequisites: CSCI201 requires CSCI101, BIOL201 requires BIOL101
        session.add(Prerequisite(
            course_code="CSCI201", prereq_code="CSCI101", min_grade="C",
        ))
        session.add(Prerequisite(
            course_code="BIOL201", prereq_code="BIOL101", min_grade="C",
        ))

        # Degree requirements for CSCI (catalog_year=2024)
        session.add(DegreeRequirement(
            major="CSCI", course_code="CSCI101", is_required=True,
            category="core", catalog_year=2024,
        ))
        session.add(DegreeRequirement(
            major="CSCI", course_code="CSCI201", is_required=True,
            category="core", catalog_year=2024,
        ))
        session.add(DegreeRequirement(
            major="CSCI", course_code="ENGL101", is_required=True,
            category="core", catalog_year=2024,
        ))
        session.add(DegreeRequirement(
            major="CSCI", course_code="MATH101", is_required=True,
            category="core", catalog_year=2024,
        ))

        # Degree requirements for BIOL (catalog_year=2024)
        session.add(DegreeRequirement(
            major="BIOL", course_code="BIOL101", is_required=True,
            category="core", catalog_year=2024,
        ))
        session.add(DegreeRequirement(
            major="BIOL", course_code="BIOL201", is_required=True,
            category="core", catalog_year=2024,
        ))
        session.add(DegreeRequirement(
            major="BIOL", course_code="ENGL101", is_required=True,
            category="core", catalog_year=2024,
        ))

        # A real student
        session.add(Student(
            id="S00001", name="Real Student", email="real@example.com",
            class_standing=ClassStanding.SO, credits_earned=30,
            major="CSCI", catalog_year=2024,
        ))

        session.commit()
        yield session


# ─────────────────────────────────────────────────────────────────
# Synthetic student ID prefix
# ─────────────────────────────────────────────────────────────────


def test_synthetic_student_ids_use_proj_prefix(seeded_session):
    """Synthetic student IDs use PROJ_ prefix."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=3, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    calc.generate_projected_demand()

    proj_students = session.exec(
        select(Student).where(Student.is_projected == True)  # noqa: E712
    ).all()

    assert len(proj_students) == 3
    for s in proj_students:
        assert s.id.startswith("PROJ_")


# ─────────────────────────────────────────────────────────────────
# Correct counts
# ─────────────────────────────────────────────────────────────────


def test_correct_number_of_students_and_demands(seeded_session):
    """Correct number of students and demands are created."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=5, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    result = calc.generate_projected_demand()

    assert result["students_created"] == 5

    # Each student gets the same set of demands (courses with no prereqs)
    proj_demands = session.exec(
        select(StudentDemand).where(
            StudentDemand.is_projected == True  # noqa: E712
        )
    ).all()

    # All students should have the same number of demands
    demand_counts = {}
    for d in proj_demands:
        demand_counts[d.student_id] = demand_counts.get(d.student_id, 0) + 1

    counts = list(demand_counts.values())
    assert len(counts) == 5
    # All should be equal
    assert len(set(counts)) == 1
    assert result["demands_created"] == 5 * counts[0]


# ─────────────────────────────────────────────────────────────────
# Freshmen only get courses with no prerequisites
# ─────────────────────────────────────────────────────────────────


def test_freshmen_only_get_no_prereq_courses(seeded_session):
    """Projected freshmen only get courses with no prerequisites."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=2, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    calc.generate_projected_demand()

    proj_demands = session.exec(
        select(StudentDemand).where(
            StudentDemand.is_projected == True  # noqa: E712
        )
    ).all()

    demand_courses = {d.course_code for d in proj_demands}

    # CSCI201 requires CSCI101, so it should NOT be in demand
    assert "CSCI201" not in demand_courses
    # CSCI101, ENGL101, MATH101 have no prereqs — should be in demand
    assert "CSCI101" in demand_courses
    assert "ENGL101" in demand_courses
    assert "MATH101" in demand_courses


# ─────────────────────────────────────────────────────────────────
# clear_projected_data preserves real students
# ─────────────────────────────────────────────────────────────────


def test_clear_projected_preserves_real_students(seeded_session):
    """clear_projected_data() preserves real students."""
    session = seeded_session

    # Generate projected data
    session.add(FreshmenProjection(
        major="CSCI", estimated_count=3, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    calc.generate_projected_demand()

    # Also create real demand
    demand_calc = DemandCalculator(session, graph)
    real_demands = demand_calc.calculate_student_demand(
        session.get(Student, "S00001")
    )
    for d in real_demands:
        session.add(d)
    session.commit()

    # Verify we have both real and projected data
    all_students = session.exec(select(Student)).all()
    assert len(all_students) == 4  # 1 real + 3 projected

    # Clear projected data
    result = calc.clear_projected_data()
    assert result["students_deleted"] == 3

    # Real student should still exist
    remaining = session.exec(select(Student)).all()
    assert len(remaining) == 1
    assert remaining[0].id == "S00001"
    assert remaining[0].is_projected is False

    # Real demands should still exist
    remaining_demands = session.exec(
        select(StudentDemand).where(
            StudentDemand.is_projected == False  # noqa: E712
        )
    ).all()
    assert len(remaining_demands) == len(real_demands)


# ─────────────────────────────────────────────────────────────────
# calculate_all_demands preserves projected data
# ─────────────────────────────────────────────────────────────────


def test_calculate_all_demands_preserves_projected(seeded_session):
    """calculate_all_demands() preserves projected data."""
    session = seeded_session

    # Generate projected data first
    session.add(FreshmenProjection(
        major="CSCI", estimated_count=2, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    proj_calc = ProjectionDemandCalculator(session, graph)
    proj_result = proj_calc.generate_projected_demand()
    proj_demand_count = proj_result["demands_created"]
    assert proj_demand_count > 0

    # Now calculate real demands (should not wipe projected)
    demand_calc = DemandCalculator(session, graph)
    demand_calc.calculate_all_demands(clear_existing=True)

    # Projected demands should still be there
    proj_demands = session.exec(
        select(StudentDemand).where(
            StudentDemand.is_projected == True  # noqa: E712
        )
    ).all()
    assert len(proj_demands) == proj_demand_count

    # Projected students should still be there
    proj_students = session.exec(
        select(Student).where(Student.is_projected == True)  # noqa: E712
    ).all()
    assert len(proj_students) == 2


# ─────────────────────────────────────────────────────────────────
# Re-generating projections is idempotent
# ─────────────────────────────────────────────────────────────────


def test_regenerate_is_idempotent(seeded_session):
    """Re-generating projections produces same result (clear + recreate)."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=4, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)

    result1 = calc.generate_projected_demand()
    result2 = calc.generate_projected_demand()

    assert result1 == result2

    # Should have exactly 4 projected students (not 8)
    proj_students = session.exec(
        select(Student).where(Student.is_projected == True)  # noqa: E712
    ).all()
    assert len(proj_students) == 4


# ─────────────────────────────────────────────────────────────────
# Multiple majors
# ─────────────────────────────────────────────────────────────────


def test_multiple_major_projections(seeded_session):
    """Projections for multiple majors create separate student pools."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=3, catalog_year=2024, is_active=True,
    ))
    session.add(FreshmenProjection(
        major="BIOL", estimated_count=2, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    result = calc.generate_projected_demand()

    assert result["students_created"] == 5  # 3 CSCI + 2 BIOL

    csci_students = session.exec(
        select(Student).where(
            Student.is_projected == True,  # noqa: E712
            Student.major == "CSCI",
        )
    ).all()
    assert len(csci_students) == 3

    biol_students = session.exec(
        select(Student).where(
            Student.is_projected == True,  # noqa: E712
            Student.major == "BIOL",
        )
    ).all()
    assert len(biol_students) == 2


# ─────────────────────────────────────────────────────────────────
# Projected demands are never graduation-critical
# ─────────────────────────────────────────────────────────────────


def test_projected_demands_not_graduation_critical(seeded_session):
    """All projected demands have is_graduation_critical=False."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=2, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    calc.generate_projected_demand()

    proj_demands = session.exec(
        select(StudentDemand).where(
            StudentDemand.is_projected == True  # noqa: E712
        )
    ).all()

    for d in proj_demands:
        assert d.is_graduation_critical is False


# ─────────────────────────────────────────────────────────────────
# Projected students are freshmen
# ─────────────────────────────────────────────────────────────────


def test_projected_students_are_freshmen(seeded_session):
    """All projected students are FR with 0 credits."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=2, catalog_year=2024, is_active=True,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    calc.generate_projected_demand()

    proj_students = session.exec(
        select(Student).where(Student.is_projected == True)  # noqa: E712
    ).all()

    for s in proj_students:
        assert s.class_standing == ClassStanding.FR
        assert s.credits_earned == 0


# ─────────────────────────────────────────────────────────────────
# CSV ingestion validates major/catalog_year
# ─────────────────────────────────────────────────────────────────


def test_csv_ingestion_validates_major(seeded_session, tmp_path):
    """CSV ingestion rejects major without degree requirements."""
    session = seeded_session

    csv_file = tmp_path / "freshmen_projections.csv"
    csv_file.write_text(
        "major,estimated_count,catalog_year\n"
        "CSCI,10,2024\n"
        "FAKE,20,2024\n"
    )

    ingestor = DataIngestor(session, tmp_path)
    projections = ingestor.load_freshmen_projections()

    # Only CSCI should be loaded; FAKE should be rejected
    assert len(projections) == 1
    assert projections[0].major == "CSCI"
    assert len(ingestor.errors) == 1
    assert "FAKE" in ingestor.errors[0]


def test_csv_ingestion_validates_count(seeded_session, tmp_path):
    """CSV ingestion rejects non-positive estimated_count."""
    session = seeded_session

    csv_file = tmp_path / "freshmen_projections.csv"
    csv_file.write_text(
        "major,estimated_count,catalog_year\n"
        "CSCI,0,2024\n"
    )

    ingestor = DataIngestor(session, tmp_path)
    projections = ingestor.load_freshmen_projections()

    assert len(projections) == 0
    assert len(ingestor.errors) == 1


# ─────────────────────────────────────────────────────────────────
# Inactive projections are skipped
# ─────────────────────────────────────────────────────────────────


def test_inactive_projections_skipped(seeded_session):
    """Inactive projections don't generate students."""
    session = seeded_session

    session.add(FreshmenProjection(
        major="CSCI", estimated_count=5, catalog_year=2024, is_active=False,
    ))
    session.commit()

    graph = PrerequisiteGraph(session)
    calc = ProjectionDemandCalculator(session, graph)
    result = calc.generate_projected_demand()

    assert result["students_created"] == 0
    assert result["demands_created"] == 0
