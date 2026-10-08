"""Tests for demand calculation."""

import tempfile
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.demand import DemandCalculator
from src.core.graph import PrerequisiteGraph
from src.database.connection import DatabaseManager
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Prerequisite,
    StructureType,
    Student,
    StudentCourseHistory,
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
def populated_db(db_manager):
    """Populate database with test data."""
    with db_manager.get_session() as session:
        # Add courses
        courses = [
            Course(
                code="CSCI101", title="Intro to CS", credits=3,
                structure_type=StructureType.STANDALONE,
            ),
            Course(
                code="CSCI201", title="Data Structures", credits=3,
                structure_type=StructureType.STANDALONE,
            ),
            Course(
                code="CSCI301", title="Algorithms", credits=3,
                structure_type=StructureType.STANDALONE,
            ),
            Course(
                code="MATH101", title="College Algebra", credits=3,
                structure_type=StructureType.STANDALONE,
            ),
        ]
        for c in courses:
            session.add(c)

        # Add prerequisites
        prereqs = [
            Prerequisite(course_code="CSCI201", prereq_code="CSCI101", min_grade="C"),
            Prerequisite(course_code="CSCI301", prereq_code="CSCI201", min_grade="C"),
            Prerequisite(course_code="CSCI201", prereq_code="MATH101", min_grade="C"),
        ]
        for p in prereqs:
            session.add(p)

        # Add students
        students = [
            Student(
                id="S001", name="Freshman", email="fr@test.com",
                class_standing=ClassStanding.FR, credits_earned=0, major="CSCI", catalog_year=2024
            ),
            Student(
                id="S002", name="Senior", email="sr@test.com",
                class_standing=ClassStanding.SR, credits_earned=90, major="CSCI", catalog_year=2024
            ),
        ]
        for s in students:
            session.add(s)

        # Add course history (senior has completed some courses)
        history = [
            StudentCourseHistory(
                student_id="S002", course_code="CSCI101", grade="B",
                semester="2023FA", credits_earned=3,
            ),
            StudentCourseHistory(
                student_id="S002", course_code="MATH101", grade="A",
                semester="2023FA", credits_earned=3,
            ),
            StudentCourseHistory(
                student_id="S002", course_code="CSCI201", grade="B",
                semester="2024SP", credits_earned=3,
            ),
        ]
        for h in history:
            session.add(h)

        # Add degree requirements
        reqs = [
            DegreeRequirement(
                major="CSCI", course_code="CSCI101", is_required=True,
                category="core", catalog_year=2024,
            ),
            DegreeRequirement(
                major="CSCI", course_code="CSCI201", is_required=True,
                category="core", catalog_year=2024,
            ),
            DegreeRequirement(
                major="CSCI", course_code="CSCI301", is_required=True,
                category="core", catalog_year=2024,
            ),
        ]
        for r in reqs:
            session.add(r)

        session.commit()

    return db_manager


def test_prerequisite_graph(populated_db):
    """Test prerequisite graph construction."""
    with populated_db.get_session() as session:
        graph = PrerequisiteGraph(session)

        # CSCI301 should require CSCI201
        prereqs = graph.get_prerequisites("CSCI301")
        assert "CSCI201" in prereqs

        # CSCI201 should require CSCI101 and MATH101
        prereqs = graph.get_prerequisites("CSCI201")
        assert "CSCI101" in prereqs
        assert "MATH101" in prereqs

        # CSCI101 has downstream courses
        dependents = graph.get_all_dependents("CSCI101")
        assert "CSCI201" in dependents
        assert "CSCI301" in dependents


def test_freshman_demand(populated_db):
    """Test demand calculation for freshman."""
    with populated_db.get_session() as session:
        graph = PrerequisiteGraph(session)
        calculator = DemandCalculator(session, graph)

        student = session.get(Student, "S001")
        demands = calculator.calculate_student_demand(student)

        # Freshman should be able to take CSCI101 and MATH101
        course_codes = {d.course_code for d in demands}
        assert "CSCI101" in course_codes
        assert "MATH101" in course_codes
        # Cannot take CSCI201 (needs CSCI101 and MATH101)
        assert "CSCI201" not in course_codes


def test_senior_demand(populated_db):
    """Test demand calculation for senior."""
    with populated_db.get_session() as session:
        graph = PrerequisiteGraph(session)
        calculator = DemandCalculator(session, graph)

        student = session.get(Student, "S002")
        demands = calculator.calculate_student_demand(student)

        # Senior has completed CSCI101, MATH101, CSCI201
        # Should be able to take CSCI301
        course_codes = {d.course_code for d in demands}
        assert "CSCI301" in course_codes
        # Should not need completed courses
        assert "CSCI101" not in course_codes


def test_urgency_scores(populated_db):
    """Test that seniors have higher urgency than freshmen."""
    with populated_db.get_session() as session:
        graph = PrerequisiteGraph(session)
        calculator = DemandCalculator(session, graph)

        freshman = session.get(Student, "S001")
        senior = session.get(Student, "S002")

        fr_demands = calculator.calculate_student_demand(freshman)
        sr_demands = calculator.calculate_student_demand(senior)

        # Get urgency for a common course they both could take
        # (if any - here we compare base scores)
        fr_score = max((d.urgency_score for d in fr_demands), default=0)
        sr_score = max((d.urgency_score for d in sr_demands), default=0)

        # Senior scores should be higher due to base score
        # (100 for SR vs 10 for FR)
        if sr_demands and fr_demands:
            # Base senior score (100) vs base freshman score (10)
            assert sr_score > fr_score


def test_calculate_all_demands(populated_db):
    """Test batch demand calculation."""
    with populated_db.get_session() as session:
        graph = PrerequisiteGraph(session)
        calculator = DemandCalculator(session, graph)

        total = calculator.calculate_all_demands(clear_existing=True)
        assert total > 0

        # Verify demands were persisted
        demands = list(session.exec(select(StudentDemand)).all())
        assert len(demands) == total
