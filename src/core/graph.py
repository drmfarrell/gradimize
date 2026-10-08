"""NetworkX prerequisite graph for Gradimize."""

import logging
from typing import Set

import networkx as nx
from sqlmodel import Session, select

from src.database.models import Course, Prerequisite

logger = logging.getLogger(__name__)


class PrerequisiteGraph:
    """Manages the directed acyclic graph of course prerequisites.

    The graph structure:
    - Nodes: Course codes
    - Edges: prereq_code -> course_code (prereq points TO the course that requires it)

    This means:
    - Descendants of a node = courses that can be taken after completing it
    - Ancestors of a node = courses that must be taken before it
    """

    def __init__(self, session: Session):
        """Initialize graph from database.

        Args:
            session: Database session.
        """
        self.session = session
        self.graph = nx.DiGraph()
        self._build_graph()

    def _build_graph(self):
        """Build the prerequisite graph from database."""
        # Add all courses as nodes
        courses = self.session.exec(select(Course)).all()
        for course in courses:
            self.graph.add_node(
                course.code,
                title=course.title,
                credits=course.credits,
                structure_type=course.structure_type.value,
            )

        # Add prerequisite edges
        prereqs = self.session.exec(select(Prerequisite)).all()
        for prereq in prereqs:
            # Edge goes from prereq TO course that requires it
            self.graph.add_edge(
                prereq.prereq_code,
                prereq.course_code,
                min_grade=prereq.min_grade,
                is_corequisite=prereq.is_corequisite,
            )

        logger.info(
            f"Built prerequisite graph: {self.graph.number_of_nodes()} courses, "
            f"{self.graph.number_of_edges()} prerequisites"
        )

        # Validate DAG property
        if not nx.is_directed_acyclic_graph(self.graph):
            cycles = list(nx.simple_cycles(self.graph))
            logger.error(f"Prerequisite graph has cycles: {cycles}")
            raise ValueError(f"Prerequisite graph contains cycles: {cycles}")

    def get_prerequisites(self, course_code: str) -> Set[str]:
        """Get all direct prerequisites for a course.

        Args:
            course_code: Course code to check.

        Returns:
            Set of prerequisite course codes.
        """
        if course_code not in self.graph:
            return set()
        return set(self.graph.predecessors(course_code))

    def get_all_prerequisites(self, course_code: str) -> Set[str]:
        """Get all prerequisites (direct and transitive) for a course.

        Args:
            course_code: Course code to check.

        Returns:
            Set of all prerequisite course codes.
        """
        if course_code not in self.graph:
            return set()
        return set(nx.ancestors(self.graph, course_code))


    def get_all_dependents(self, course_code: str) -> Set[str]:
        """Get all courses that require this course (direct and transitive).

        Args:
            course_code: Course code to check.

        Returns:
            Set of all course codes that require this course.
        """
        if course_code not in self.graph:
            return set()
        return set(nx.descendants(self.graph, course_code))

    def can_take_course(self, course_code: str, completed_courses: Set[str]) -> bool:
        """Check if a student can take a course given completed courses.

        Args:
            course_code: Course to check.
            completed_courses: Set of courses already completed.

        Returns:
            True if all prerequisites are satisfied.
        """
        prereqs = self.get_prerequisites(course_code)
        return prereqs.issubset(completed_courses)

    def get_available_courses(self, completed_courses: Set[str]) -> Set[str]:
        """Get all courses a student can take given completed courses.

        Args:
            completed_courses: Set of courses already completed.

        Returns:
            Set of available course codes.
        """
        available = set()
        for course in self.graph.nodes():
            if course not in completed_courses:
                if self.can_take_course(course, completed_courses):
                    available.add(course)
        return available

    def get_bottleneck_courses(self, min_dependents: int = 3) -> list[tuple[str, int]]:
        """Identify bottleneck courses that unlock many other courses.

        Args:
            min_dependents: Minimum number of dependents to be a bottleneck.

        Returns:
            List of (course_code, dependent_count) tuples, sorted by count descending.
        """
        bottlenecks = []
        for course in self.graph.nodes():
            dependent_count = len(self.get_all_dependents(course))
            if dependent_count >= min_dependents:
                bottlenecks.append((course, dependent_count))

        bottlenecks.sort(key=lambda x: x[1], reverse=True)
        return bottlenecks


    def get_corequisites(self, course_code: str) -> Set[str]:
        """Get courses that can be taken concurrently with this course.

        Args:
            course_code: Course to check.

        Returns:
            Set of corequisite course codes.
        """
        coreqs = set()
        if course_code not in self.graph:
            return coreqs

        for pred in self.graph.predecessors(course_code):
            edge_data = self.graph.get_edge_data(pred, course_code)
            if edge_data and edge_data.get("is_corequisite"):
                coreqs.add(pred)

        return coreqs

