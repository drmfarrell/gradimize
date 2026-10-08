"""The demand co-occurrence matrix fed INTO the solver.

Despite the name, this module does NOT inspect a solved schedule for
conflicts — for room double-books, faculty overlaps and student clashes in
an existing schedule, see ``validate.py``. This module runs BEFORE the
solve: it scores every pair of courses by how much urgency-weighted student
demand overlaps between them (a student who needs both), so the engine knows
which pairs it should try hardest to keep out of the same time slot. It also
holds ``TimeSlotOverlapCalculator``, the pure slot-vs-slot geometry the
engine consults.
"""

import logging
import threading
from collections import defaultdict

import pandas as pd
from sqlalchemy import func
from sqlmodel import Session, select

from src.core.timeutil import slots_overlap, time_to_minutes
from src.database.models import StudentDemand, TimeSlot

logger = logging.getLogger(__name__)


class ConflictMatrix:
    """Manages the course conflict matrix based on student demand."""

    def __init__(self, session: Session):
        """Initialize conflict matrix.

        Args:
            session: Database session.
        """
        self.session = session
        self.matrix: pd.DataFrame | None = None

    def build_matrix(self) -> pd.DataFrame:
        """Build Course x Course conflict matrix.

        Cell value = sum of minimum urgency scores for students
        who need BOTH courses.

        Returns:
            DataFrame with course codes as index and columns.
        """
        # Get all demands
        demands = self.session.exec(select(StudentDemand)).all()

        if not demands:
            logger.warning("No student demands found - conflict matrix will be empty")
            # Must still assign self.matrix: every accessor dereferences it
            # after an `if self.matrix is None: self.build_matrix()` guard.
            self.matrix = pd.DataFrame()
            return self.matrix

        # Get unique courses
        courses = sorted(set(d.course_code for d in demands))

        # Group demands by student
        by_student: dict[str, list[StudentDemand]] = defaultdict(list)
        for d in demands:
            by_student[d.student_id].append(d)

        # Accumulate pair weights in a plain dict, then build the DataFrame
        # once. Per-pair scalar `DataFrame.loc +=` costs tens of microseconds
        # each; at 5,000 students × ~30-60 demands that's millions of calls
        # (minutes) for what should take seconds.
        pair_weights: dict[tuple[str, str], float] = defaultdict(float)
        for student_demands in by_student.values():
            for i, d1 in enumerate(student_demands):
                for d2 in student_demands[i + 1 :]:
                    # Use minimum urgency of the pair
                    conflict_weight = min(d1.urgency_score, d2.urgency_score)
                    pair_weights[d1.course_code, d2.course_code] += conflict_weight

        self.matrix = pd.DataFrame(0.0, index=courses, columns=courses)
        for (c1, c2), w in pair_weights.items():
            self.matrix.at[c1, c2] += w
            self.matrix.at[c2, c1] += w

        logger.info(
            f"Built conflict matrix: {len(courses)} courses, "
            f"{(self.matrix > 0).sum().sum() // 2} non-zero pairs"
        )

        return self.matrix

    def get_high_conflict_pairs(
        self, threshold: float = 50.0
    ) -> list[tuple[str, str, float]]:
        """Get course pairs with conflict above threshold.

        Args:
            threshold: Minimum conflict score to include.

        Returns:
            List of (course1, course2, conflict_score) tuples.
        """
        if self.matrix is None:
            self.build_matrix()

        pairs = []
        courses = list(self.matrix.index)

        for i, c1 in enumerate(courses):
            for c2 in courses[i + 1 :]:
                score = self.matrix.loc[c1, c2]
                if score >= threshold:
                    pairs.append((c1, c2, score))

        pairs.sort(key=lambda x: x[2], reverse=True)
        return pairs

    def get_conflict_score(self, course1: str, course2: str) -> float:
        """Get conflict score between two courses.

        Args:
            course1: First course code.
            course2: Second course code.

        Returns:
            Conflict score (0 if no conflict or courses not in matrix).
        """
        if self.matrix is None:
            self.build_matrix()

        if course1 not in self.matrix.index or course2 not in self.matrix.columns:
            return 0.0

        return float(self.matrix.loc[course1, course2])

    def get_top_conflicts_for_course(
        self, course_code: str, n: int = 10
    ) -> list[tuple[str, float]]:
        """Get courses with highest conflict scores for a given course.

        Args:
            course_code: Course to check.
            n: Number of top conflicts to return.

        Returns:
            List of (course_code, score) tuples.
        """
        if self.matrix is None:
            self.build_matrix()

        if course_code not in self.matrix.index:
            return []

        conflicts = self.matrix.loc[course_code].sort_values(ascending=False)
        return [(code, score) for code, score in conflicts.head(n).items() if score > 0]


class TimeSlotOverlapCalculator:
    """Precomputes time slot overlaps."""

    def __init__(self, session: Session):
        """Initialize overlap calculator.

        Args:
            session: Database session.
        """
        self.session = session
        self.overlaps: dict[int, set[int]] = {}
        self.adjacencies: dict[int, set[int]] = {}
        self.tight_adjacencies: dict[int, set[int]] = {}

    def compute_overlaps(self) -> dict[int, set[int]]:
        """Compute which time slots overlap with each other.

        Two slots overlap if:
        1. They share at least one day AND
        2. Their time ranges intersect

        Returns:
            Dictionary mapping slot_id -> set of overlapping slot_ids.
        """
        slots = self.session.exec(select(TimeSlot)).all()

        for slot in slots:
            self.overlaps[slot.id] = set()

        for i, slot1 in enumerate(slots):
            for slot2 in slots[i + 1 :]:
                if slots_overlap(slot1, slot2):
                    self.overlaps[slot1.id].add(slot2.id)
                    self.overlaps[slot2.id].add(slot1.id)

        total_overlaps = sum(len(v) for v in self.overlaps.values()) // 2
        logger.info(f"Computed overlaps for {len(slots)} slots: {total_overlaps} pairs")

        return self.overlaps

    def compute_adjacencies(self, max_gap_minutes: int = 15) -> dict[int, set[int]]:
        """Compute which time slots are adjacent (back-to-back).

        Two slots are adjacent if they share a day, do NOT overlap,
        and the gap between end of one and start of the other is <= max_gap_minutes.

        Args:
            max_gap_minutes: Maximum gap in minutes to consider adjacent.

        Returns:
            Dictionary mapping slot_id -> set of adjacent slot_ids.
        """
        slots = self.session.exec(select(TimeSlot)).all()

        for slot in slots:
            self.adjacencies[slot.id] = set()

        for i, slot1 in enumerate(slots):
            for slot2 in slots[i + 1 :]:
                if self._slots_adjacent(slot1, slot2, max_gap_minutes):
                    self.adjacencies[slot1.id].add(slot2.id)
                    self.adjacencies[slot2.id].add(slot1.id)

        total = sum(len(v) for v in self.adjacencies.values()) // 2
        logger.info(
            f"Computed adjacencies for {len(slots)} slots: {total} pairs "
            f"(max_gap={max_gap_minutes}min)"
        )

        return self.adjacencies

    def compute_tight_adjacencies(self, min_travel_minutes: int = 10) -> dict[int, set[int]]:
        """Compute slot pairs where the gap is too short for travel.

        A subset of adjacencies where gap < min_travel_minutes.

        Args:
            min_travel_minutes: Minimum travel time threshold.

        Returns:
            Dictionary mapping slot_id -> set of tight-adjacent slot_ids.
        """
        slots = self.session.exec(select(TimeSlot)).all()

        for slot in slots:
            self.tight_adjacencies[slot.id] = set()

        for i, slot1 in enumerate(slots):
            for slot2 in slots[i + 1 :]:
                if self._slots_tight_adjacent(slot1, slot2, min_travel_minutes):
                    self.tight_adjacencies[slot1.id].add(slot2.id)
                    self.tight_adjacencies[slot2.id].add(slot1.id)

        total = sum(len(v) for v in self.tight_adjacencies.values()) // 2
        logger.info(
            f"Computed tight adjacencies: {total} pairs (min_travel={min_travel_minutes}min)"
        )

        return self.tight_adjacencies

    def _slots_adjacent(self, slot1: TimeSlot, slot2: TimeSlot, max_gap_minutes: int) -> bool:
        """Check if two slots are adjacent (back-to-back within max_gap)."""
        days1 = set(slot1.days)
        days2 = set(slot2.days)
        if not days1.intersection(days2):
            return False

        start1 = time_to_minutes(slot1.start_time)
        end1 = time_to_minutes(slot1.end_time)
        start2 = time_to_minutes(slot2.start_time)
        end2 = time_to_minutes(slot2.end_time)

        # Must not overlap
        if start1 < end2 and start2 < end1:
            return False

        # Gap = start of later - end of earlier
        gap = min(abs(start2 - end1), abs(start1 - end2))
        return gap <= max_gap_minutes

    def _slots_tight_adjacent(
        self, slot1: TimeSlot, slot2: TimeSlot, min_travel_minutes: int
    ) -> bool:
        """Check if two slots have a gap too short for travel between buildings."""
        days1 = set(slot1.days)
        days2 = set(slot2.days)
        if not days1.intersection(days2):
            return False

        start1 = time_to_minutes(slot1.start_time)
        end1 = time_to_minutes(slot1.end_time)
        start2 = time_to_minutes(slot2.start_time)
        end2 = time_to_minutes(slot2.end_time)

        # Must not overlap
        if start1 < end2 and start2 < end1:
            return False

        gap = min(abs(start2 - end1), abs(start1 - end2))
        return gap < min_travel_minutes

    def get_overlapping_slots(self, slot_id: int) -> set[int]:
        """Get all slots that overlap with a given slot.

        Args:
            slot_id: Time slot ID.

        Returns:
            Set of overlapping slot IDs.
        """
        if not self.overlaps:
            self.compute_overlaps()
        return self.overlaps.get(slot_id, set())


def build_conflict_matrix(session: Session) -> ConflictMatrix:
    """Build conflict matrix from session.

    Args:
        session: Database session.

    Returns:
        ConflictMatrix instance with matrix computed.
    """
    matrix = ConflictMatrix(session)
    matrix.build_matrix()
    return matrix


# ─────────────────────────────────────────────────────────────────
# Process-wide cache of the built matrix
#
# Building the matrix scans every student's demand and fills an N×N course
# matrix — expensive, but it only changes when StudentDemand changes. We cache
# the built matrix in-process keyed on a cheap signature of the demand table
# and rebuild only when that signature changes. Guarded so concurrent requests
# don't each trigger a rebuild; the first request pays the build cost, the rest
# reuse it. Lives here (not in a route module) so every caller — the web API
# and the TUI alike — shares one cache.
# ─────────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: dict = {"signature": None, "matrix": None}


def _demand_signature(session: Session) -> tuple[int, int]:
    """Cheap fingerprint of StudentDemand: (row count, max id).

    Changes whenever demands are added, removed, or reloaded (ids autoincrement),
    so the cached matrix invalidates automatically when the inputs change.
    """
    count, max_id = session.exec(
        select(func.count(StudentDemand.id), func.max(StudentDemand.id))
    ).one()
    return (count or 0, max_id or 0)


def get_conflict_matrix_cached(session: Session) -> ConflictMatrix:
    """Return a built ConflictMatrix, rebuilding only when demands changed."""
    signature = _demand_signature(session)
    with _cache_lock:
        if _cache["signature"] == signature and _cache["matrix"] is not None:
            return _cache["matrix"]
        matrix = build_conflict_matrix(session)
        _cache["signature"] = signature
        _cache["matrix"] = matrix
        return matrix

