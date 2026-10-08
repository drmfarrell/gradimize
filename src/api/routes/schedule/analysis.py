"""Read-only schedule analysis: diff, viability, student quality, rebalance, explanations,
conflict check.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.core import advisor, job_lock
from src.core.schedule_diff import diff_schedules as compute_schedule_diff
from src.core.validate import check_schedule_conflicts
from src.database.connection import get_session
from src.database.models import (
    ScheduleVersion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedule", tags=["schedule"])


@router.get("/{schedule_id}/diff/{other_id}")
def diff_schedules(
    schedule_id: int, other_id: int, session: Session = Depends(get_session)
):
    """Compare two schedule versions, keyed by (course_code, section_number).

    Reports sections added/removed and, for sections in both, changes to
    time slot, room, and instructor (meeting 0). This is how a registrar
    answers "what did the re-solve actually change?".
    """
    try:
        return compute_schedule_diff(session, schedule_id, base_id=other_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


class SectionViabilityInfo(BaseModel):
    section_id: int
    crn: str
    course_code: str
    section_number: str
    enrolled: int
    capacity: int
    min_enrollment: int
    reseatable: int
    stranded: int
    stranded_grad_critical: int
    stranded_students: list[str]
    alternatives: list[dict]
    next_offering: str | None
    recommendation: str


class ViabilityReportInfo(BaseModel):
    schedule_id: int
    semester: str
    total_sections: int
    below_min: list[SectionViabilityInfo]


@router.get("/{schedule_id}/viability", response_model=ViabilityReportInfo)
def schedule_viability(
    schedule_id: int, session: Session = Depends(get_session)
):
    """Below-minimum sections and the real impact of cancelling each.

    Reseating is seat- AND time-conflict-aware; graduation-critical
    students are reseated first, so stranded counts are best-case.
    """
    from src.core.viability import analyze_schedule_viability

    try:
        report = analyze_schedule_viability(session, schedule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ViabilityReportInfo(
        schedule_id=report.schedule_id,
        semester=report.semester,
        total_sections=report.total_sections,
        below_min=[SectionViabilityInfo(**vars(sv)) for sv in report.below_min],
    )


class StudentQualityInfo(BaseModel):
    student_id: str
    days_on_campus: int
    total_gap_minutes: int
    longest_gap_minutes: int
    max_marathon_minutes: int
    marathons_over_3h: int
    heavy_days: int
    earliest_start: str | None
    latest_end: str | None
    badness: float
    lunch_crunch_days: int
    longest_workday_minutes: int
    long_workdays: int


class DepartmentPrimeTimeInfo(BaseModel):
    department: str
    total_meetings: int
    prime_meetings: int
    prime_share: float


class StudentQualityReportInfo(BaseModel):
    schedule_id: int
    semester: str
    total_students: int
    mean_days_on_campus: float
    median_days_on_campus: float
    mean_gap_minutes: float
    students_5_day: int
    students_big_gap: int
    students_heavy_marathon: int
    students_no_lunch: int
    students_long_workday: int
    worst_students: list[StudentQualityInfo]
    department_prime_time: list[DepartmentPrimeTimeInfo]


@router.get("/{schedule_id}/student-quality", response_model=StudentQualityReportInfo)
def schedule_student_quality(
    schedule_id: int, session: Session = Depends(get_session)
):
    """How livable is each enrolled student's week: daily gaps, marathon
    stretches, day-on-campus spread, and the worst 20 by a badness score —
    plus a department-level prime-time fairness readout.
    """
    from src.core.schedule_quality import analyze_student_quality

    try:
        report = analyze_student_quality(session, schedule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return StudentQualityReportInfo(
        schedule_id=report.schedule_id,
        semester=report.semester,
        total_students=report.total_students,
        mean_days_on_campus=report.mean_days_on_campus,
        median_days_on_campus=report.median_days_on_campus,
        mean_gap_minutes=report.mean_gap_minutes,
        students_5_day=report.students_5_day,
        students_big_gap=report.students_big_gap,
        students_heavy_marathon=report.students_heavy_marathon,
        students_no_lunch=report.students_no_lunch,
        students_long_workday=report.students_long_workday,
        worst_students=[StudentQualityInfo(**vars(sq)) for sq in report.worst_students],
        department_prime_time=[
            DepartmentPrimeTimeInfo(**vars(d)) for d in report.department_prime_time
        ],
    )


class RebalanceResponse(BaseModel):
    schedule_id: int
    default_mode: str
    moves: int
    students_improved: int
    passes: int
    mean_badness_before: float
    mean_badness_after: float
    moved_students: list[str]


@router.post("/{schedule_id}/rebalance", response_model=RebalanceResponse)
def rebalance_schedule_seats(
    schedule_id: int,
    mode: str = Query("balanced", pattern="^(balanced|cluster|spread)$"),
    session: Session = Depends(get_session),
):
    """Reassign students among sibling sections to fit their week shape.

    Stage 2 of scheduling: the timetable and capacities stay fixed; only
    which student sits in which sibling section changes, honoring each
    student's pacing_preference (cluster/spread) with `mode` as the
    default for everyone else. Safe moves only (free seat, no new time
    conflicts). Mutates enrollments — 409 while a solve is running.
    """
    from src.core.seatswap import rebalance_seats

    if not job_lock.try_acquire("rebalance", f"Rebalance schedule {schedule_id}"):
        raise HTTPException(status_code=409, detail=job_lock.BUSY_MESSAGE)
    try:
        result = rebalance_seats(session, schedule_id, default_mode=mode)
    except ValueError:
        raise HTTPException(status_code=404, detail="Schedule not found")
    finally:
        job_lock.release()
    return RebalanceResponse(**vars(result))


@router.get("/{schedule_id}/sections/{section_id}/explain")
def explain_section(
    schedule_id: int, section_id: int, session: Session = Depends(get_session)
):
    """Human-readable reasons behind a section's placement (trust feature)."""
    try:
        return advisor.explain_section(session, schedule_id, section_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{schedule_id}/sections/{section_id}/explain-move")
def explain_section_move(
    schedule_id: int,
    section_id: int,
    slot_id: int = Query(..., ge=1),
    session: Session = Depends(get_session),
):
    """Counterfactual: what breaks if this section moved to `slot_id`?

    Answers the advising question "why CAN'T this class be at 10am" —
    hard blockers (slot type, blocked periods, instructor availability and
    teaching clashes, no free compatible room) plus the soft price
    (enrolled students whose other classes collide). Static analysis of
    the stored schedule; no solver run.
    """
    try:
        return advisor.explain_move(session, schedule_id, section_id, slot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class ConflictSectionOut(BaseModel):
    id: int
    crn: str
    course_code: str
    section_number: str


class ConflictOut(BaseModel):
    kind: str
    detail: str
    sections: list[ConflictSectionOut]
    affected_students: int = 0


class CheckConflictsResponse(BaseModel):
    schedule_id: int
    total: int
    room: int
    faculty: int
    student: int
    conflicts: list[ConflictOut]


@router.get("/{schedule_id}/check-conflicts", response_model=CheckConflictsResponse)
def check_conflicts(  # sync def -> threadpool; pairwise scan is CPU-heavy
    schedule_id: int, session: Session = Depends(get_session)
):
    """Validate the schedule's current assignments for hard conflicts."""
    if not session.get(ScheduleVersion, schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    report = check_schedule_conflicts(session, schedule_id)
    # Prioritize the actionable hard conflicts (room, faculty) ahead of the
    # soft student overlaps, and cap the list to keep the payload sane.
    order = {"room": 0, "faculty": 1, "student": 2}
    ordered = sorted(report.conflicts, key=lambda c: order.get(c.kind, 9))[:200]
    return CheckConflictsResponse(
        schedule_id=schedule_id,
        total=report.total,
        room=report.room,
        faculty=report.faculty,
        student=report.student,
        conflicts=[
            ConflictOut(
                kind=c.kind,
                detail=c.detail,
                sections=[
                    ConflictSectionOut(
                        id=s.id, crn=s.crn, course_code=s.course_code,
                        section_number=s.section_number,
                    )
                    for s in c.sections
                ],
                affected_students=c.affected_students,
            )
            for c in ordered
        ],
    )
