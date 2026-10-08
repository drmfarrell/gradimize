"""Section viability: which sections are below minimum enrollment, and what
actually happens if you cancel them.

Small institutions live on this decision: a 4-student section is expensive
to run, but cancelling it may strand students — especially seniors — for a
full year (or two, for biennial courses). For every below-minimum section
this module answers the operative question: **who can move, who gets
stranded, and when is the next chance?**

Reseating is checked honestly: a sibling section only counts as an
alternative for a student if it has a free seat AND doesn't collide with
the rest of that student's schedule. Graduation-critical students are
reseated first, so the stranded numbers are best-case.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, select

from src.core.cadence import is_off_cadence, next_term
from src.core.timeutil import parse_semester, slots_overlap
from src.database.models import (
    Course,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    StudentDemand,
    TimeSlot,
)

logger = logging.getLogger(__name__)

NEXT_OFFERING_SEARCH_TERMS = 8  # four years of regular terms


@dataclass
class SectionViability:
    section_id: int
    crn: str
    course_code: str
    section_number: str
    enrolled: int
    capacity: int
    min_enrollment: int
    reseatable: int = 0
    stranded: int = 0
    stranded_grad_critical: int = 0
    stranded_students: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    next_offering: str | None = None
    recommendation: str = ""


@dataclass
class ViabilityReport:
    schedule_id: int
    semester: str
    total_sections: int
    below_min: list[SectionViability] = field(default_factory=list)


def _find_next_offering(course: Course, current_semester: str) -> str | None:
    """First term AFTER the current one where the course is on-cadence."""
    try:
        term = next_term(current_semester)
    except ValueError:
        return None
    for _ in range(NEXT_OFFERING_SEARCH_TERMS):
        year, term_name = parse_semester(term)
        if not is_off_cadence(course, year, term_name):
            return term
        term = next_term(term)
    return None


def analyze_schedule_viability(
    session: Session, schedule_id: int
) -> ViabilityReport:
    """Analyze every below-minimum section of a schedule."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if schedule is None:
        raise ValueError(f"Schedule {schedule_id} not found")

    sections = list(session.exec(
        select(Section).where(Section.schedule_id == schedule_id)
    ).all())
    section_ids = {s.id for s in sections}
    enrollments = [
        e for e in session.exec(select(SectionEnrollment)).all()
        if e.section_id in section_ids
    ]
    meetings = [
        m for m in session.exec(select(SectionMeeting)).all()
        if m.section_id in section_ids
    ]
    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
    courses = {c.code: c for c in session.exec(select(Course)).all()}
    grad_critical_pairs = {
        (d.student_id, d.course_code)
        for d in session.exec(select(StudentDemand)).all()
        if d.is_graduation_critical
    }

    by_section_students: dict[int, list[str]] = defaultdict(list)
    student_sections: dict[str, list[int]] = defaultdict(list)
    for e in enrollments:
        by_section_students[e.section_id].append(e.student_id)
        student_sections[e.student_id].append(e.section_id)

    section_slots: dict[int, list[TimeSlot]] = defaultdict(list)
    for m in meetings:
        slot = slots.get(m.time_slot_id)
        if slot is not None:
            section_slots[m.section_id].append(slot)

    sections_by_course: dict[str, list[Section]] = defaultdict(list)
    for s in sections:
        sections_by_course[s.course_code].append(s)

    report = ViabilityReport(
        schedule_id=schedule_id,
        semester=schedule.semester,
        total_sections=len(sections),
    )

    for s in sections:
        course = courses.get(s.course_code)
        min_enroll = course.min_enrollment if course else 5
        enrolled = len(by_section_students.get(s.id, []))
        if enrolled >= min_enroll:
            continue

        sv = SectionViability(
            section_id=s.id,
            crn=s.crn,
            course_code=s.course_code,
            section_number=s.section_number,
            enrolled=enrolled,
            capacity=s.capacity,
            min_enrollment=min_enroll,
        )

        siblings = [
            sib for sib in sections_by_course[s.course_code] if sib.id != s.id
        ]
        seats_free = {
            sib.id: max(0, sib.capacity - len(by_section_students.get(sib.id, [])))
            for sib in siblings
        }
        sv.alternatives = [
            {
                "crn": sib.crn,
                "section_number": sib.section_number,
                "seats_free": seats_free[sib.id],
            }
            for sib in siblings
            if seats_free[sib.id] > 0
        ]

        # Best-case reseating: graduation-critical students first; a sibling
        # only works if a seat remains AND its meetings don't collide with
        # the rest of the student's schedule.
        students = sorted(
            by_section_students.get(s.id, []),
            key=lambda sid: (sid, s.course_code) not in grad_critical_pairs,
        )
        remaining = dict(seats_free)
        for sid in students:
            other_slots = [
                slot
                for sec_id in student_sections[sid]
                if sec_id != s.id
                for slot in section_slots.get(sec_id, [])
            ]
            placed = False
            for sib in siblings:
                if remaining.get(sib.id, 0) <= 0:
                    continue
                if any(
                    slots_overlap(a, b)
                    for a in section_slots.get(sib.id, [])
                    for b in other_slots
                ):
                    continue
                remaining[sib.id] -= 1
                placed = True
                break
            if placed:
                sv.reseatable += 1
            else:
                sv.stranded += 1
                sv.stranded_students.append(sid)
                if (sid, s.course_code) in grad_critical_pairs:
                    sv.stranded_grad_critical += 1

        if course is not None:
            sv.next_offering = _find_next_offering(course, schedule.semester)

        if enrolled == 0:
            sv.recommendation = "Empty section — safe to cancel."
        elif sv.stranded == 0:
            sv.recommendation = (
                f"Safe to cancel: all {enrolled} student(s) can reseat into "
                f"sibling sections without schedule conflicts."
            )
        else:
            parts = [
                f"Cancelling strands {sv.stranded} of {enrolled} student(s)"
            ]
            if sv.stranded_grad_critical:
                parts.append(
                    f"{sv.stranded_grad_critical} graduation-critical"
                )
            msg = ", ".join(parts) + "."
            if sv.next_offering:
                msg += f" Next offering: {sv.next_offering}."
            if sv.stranded_grad_critical:
                msg += " Do not cancel without an advising review."
            sv.recommendation = msg

        report.below_min.append(sv)

    return report
