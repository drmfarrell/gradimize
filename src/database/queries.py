"""Common database queries for Gradimize.

Only queries with live callers belong here — an earlier version carried 30+
unused wrappers while routes and screens wrote the same selects inline.
"""

from sqlmodel import Session, select

from src.database.models import (
    SemesterGuide,
    SemesterGuideEntry,
    StudentCourseHistory,
)

# Ordered letter grades, best first. Shared with doctor.py's KNOWN_GRADES so
# the "what counts as passing" and "what grades are recognized" sets can't
# drift apart.
LETTER_GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-"]

# Grade codes that grant credit without being letter grades: transfer (TR/T),
# pass (P), credit-received (CR), satisfactory (S). Real extracts — including
# the SHRTRCE transfer rows the data request explicitly asks for — carry these
# instead of letters, and they must count as "completed" for prerequisite
# checks. They satisfy ANY min-grade threshold (institutional convention:
# transfer/pass credit fulfills prerequisites unless a policy says otherwise).
# Deliberately excluded: AU, W, WF, WP, I, F, NP, U, NC — no credit granted.
CREDIT_BEARING_NON_LETTER_GRADES = {"TR", "T", "P", "CR", "S"}


def get_student_history(session: Session, student_id: str) -> list[StudentCourseHistory]:
    """Get all completed courses for a student."""
    return list(
        session.exec(
            select(StudentCourseHistory).where(StudentCourseHistory.student_id == student_id)
        ).all()
    )


def get_completed_courses(session: Session, student_id: str, passing_grade: str = "D") -> set[str]:
    """Get course codes a student has passed.

    Args:
        session: Database session.
        student_id: Student ID.
        passing_grade: Minimum passing grade (default "D").

    Returns:
        Set of course codes the student has passed.
    """
    # One rule for every threshold: grades at or above passing_grade pass.
    # (An earlier special case for the default made "D" include "D-" while an
    # explicit passing_grade="D" excluded it.) Credit-bearing non-letter codes
    # (transfer/pass/credit) satisfy any threshold — see the constant above.
    try:
        idx = LETTER_GRADE_ORDER.index(passing_grade)
    except ValueError:
        idx = LETTER_GRADE_ORDER.index("D")
    passing_grades = set(LETTER_GRADE_ORDER[: idx + 1])

    history = get_student_history(session, student_id)
    completed = set()
    for h in history:
        grade = (h.grade or "").strip().upper()
        if grade in passing_grades or grade in CREDIT_BEARING_NON_LETTER_GRADES:
            completed.add(h.course_code)
    return completed


def get_guide_for_student(
    session: Session, major: str, catalog_year: int, concentration: str | None = None
) -> SemesterGuide | None:
    """Get the best-matching semester guide for a student.

    Prefers concentration-specific guide, falls back to base (concentration=NULL).

    Args:
        session: Database session.
        major: Student's major.
        catalog_year: Student's catalog year.
        concentration: Student's concentration (or None).

    Returns:
        Best matching SemesterGuide, or None.
    """
    if concentration:
        conc_guide = session.exec(
            select(SemesterGuide).where(
                SemesterGuide.major == major,
                SemesterGuide.catalog_year == catalog_year,
                SemesterGuide.concentration == concentration,
            )
        ).first()
        if conc_guide:
            return conc_guide

    # Fall back to base guide (no concentration)
    return session.exec(
        select(SemesterGuide).where(
            SemesterGuide.major == major,
            SemesterGuide.catalog_year == catalog_year,
            SemesterGuide.concentration == None,  # noqa: E711
        )
    ).first()


def get_guide_entries(session: Session, guide_id: int) -> list[SemesterGuideEntry]:
    """Get all entries for a semester guide, ordered by semester_number.

    Args:
        session: Database session.
        guide_id: ID of the semester guide.

    Returns:
        List of SemesterGuideEntry records.
    """
    return list(
        session.exec(
            select(SemesterGuideEntry)
            .where(SemesterGuideEntry.guide_id == guide_id)
            .order_by(SemesterGuideEntry.semester_number)  # type: ignore
        ).all()
    )
