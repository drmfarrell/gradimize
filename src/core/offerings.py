"""Infer Course.offer_terms / Course.cadence from historical schedules.

Walks every existing ScheduleVersion + Section row and aggregates which
terms/years each course has been offered in. Updates the Course rows in
place.

This is a soft prior: if a course has never appeared, leave it alone. If
the user later types in offer_terms/cadence by hand on a course, re-running
inference will overwrite — call only when you actually want to overwrite.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from sqlmodel import Session, select

from src.core.timeutil import parse_semester
from src.database.models import Course, ScheduleVersion, Section

logger = logging.getLogger(__name__)


@dataclass
class CourseInferenceResult:
    course_code: str
    observed_terms: set[str] = field(default_factory=set)
    observed_years: set[int] = field(default_factory=set)
    new_offer_terms: list[str] = field(default_factory=list)
    new_cadence: str = "ANNUAL"
    changed: bool = False
    note: Optional[str] = None


@dataclass
class InferenceReport:
    courses_examined: int = 0
    courses_with_history: int = 0
    courses_changed: int = 0
    schedules_examined: int = 0
    skipped_unparseable_semesters: list[str] = field(default_factory=list)
    results: list[CourseInferenceResult] = field(default_factory=list)


def infer_course_offerings(
    session: Session,
    biennial_min_years: int = 2,
    apply: bool = True,
) -> InferenceReport:
    """Infer offer_terms and cadence from historical sections.

    Args:
        session: DB session.
        biennial_min_years: Minimum number of distinct years observed before
            we'll claim biennial-even/odd. With 1 year of history we can't
            distinguish biennial from annual, so default ANNUAL.
        apply: If True, write changes to the DB. If False, return what would
            change without committing (dry run).

    Returns:
        InferenceReport summarizing per-course findings.
    """
    report = InferenceReport()

    # Collect (year, term) per course across all schedules
    schedules = list(session.exec(select(ScheduleVersion)).all())
    report.schedules_examined = len(schedules)

    schedule_year_term: dict[int, tuple[int, str]] = {}
    for sv in schedules:
        try:
            schedule_year_term[sv.id] = parse_semester(sv.semester)
        except ValueError:
            report.skipped_unparseable_semesters.append(sv.semester)

    if not schedule_year_term:
        logger.info("No parseable semesters in schedule history; nothing to infer")
        return report

    sections = list(session.exec(
        select(Section).where(Section.schedule_id.in_(list(schedule_year_term.keys())))
    ).all())

    course_to_year_terms: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for sec in sections:
        yt = schedule_year_term.get(sec.schedule_id)
        if yt is None:
            continue
        course_to_year_terms[sec.course_code].append(yt)

    courses = list(session.exec(select(Course)).all())
    report.courses_examined = len(courses)

    for course in courses:
        ys_ts = course_to_year_terms.get(course.code, [])
        if not ys_ts:
            continue
        report.courses_with_history += 1

        observed_terms = {t for _, t in ys_ts}
        observed_years = {y for y, _ in ys_ts}

        new_terms = sorted(observed_terms)

        new_cadence = "ANNUAL"
        if len(observed_years) >= biennial_min_years:
            parities = {y % 2 for y in observed_years}
            if parities == {0}:
                new_cadence = "BIENNIAL_EVEN"
            elif parities == {1}:
                new_cadence = "BIENNIAL_ODD"

        result = CourseInferenceResult(
            course_code=course.code,
            observed_terms=observed_terms,
            observed_years=observed_years,
            new_offer_terms=new_terms,
            new_cadence=new_cadence,
        )
        old_terms = sorted(course.offer_terms or [])
        old_cadence = course.cadence or "ANNUAL"
        if old_terms != new_terms or old_cadence != new_cadence:
            result.changed = True
            result.note = (
                f"{old_terms or 'any'}/{old_cadence} -> "
                f"{new_terms}/{new_cadence}"
            )
            report.courses_changed += 1
            if apply:
                course.offer_terms = new_terms
                course.cadence = new_cadence
                session.add(course)

        report.results.append(result)

    if apply:
        session.commit()

    logger.info(
        "Inference complete: %d courses examined, %d with history, %d changed",
        report.courses_examined, report.courses_with_history, report.courses_changed,
    )
    return report
