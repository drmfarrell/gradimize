"""Academic-calendar arithmetic: term sequencing, class standing, cadence.

Small pure helpers about *when things happen* in the academic year, shared by
the solver, the what-if runners, and the analyzers. Previously these were
scattered as underscore-privates: ``_is_off_cadence`` lived in the engine and
was reached into by four modules, while ``next_term`` / ``derive_standing``
lived in the heavyweight ``longrange`` simulation and were imported from there
just for term math. One public home instead.
"""

from src.core.timeutil import parse_semester
from src.database.models import ClassStanding, Course


def is_off_cadence(course: Course, year: int, term_name: str) -> bool:
    """Return True if course is NOT normally offered in this (year, term)."""
    if course.offer_terms and term_name not in course.offer_terms:
        return True
    if course.cadence == "BIENNIAL_EVEN" and (year % 2) != 0:
        return True
    if course.cadence == "BIENNIAL_ODD" and (year % 2) == 0:
        return True
    return False


def next_term(semester: str) -> str:
    """The following regular term: 2026FA -> 2027SP -> 2027FA (no summer)."""
    year, term = parse_semester(semester)
    if term == "FALL":
        return f"{year + 1}SP"
    return f"{year}FA"


def derive_standing(credits_earned: int) -> ClassStanding:
    """Class standing implied by earned credits (FR/SO/JR/SR thresholds)."""
    if credits_earned < 30:
        return ClassStanding.FR
    if credits_earned < 60:
        return ClassStanding.SO
    if credits_earned < 90:
        return ClassStanding.JR
    return ClassStanding.SR
