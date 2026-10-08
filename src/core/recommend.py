"""Schedule recommendation engine for Gradimize."""

import logging
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from sqlmodel import Session, select

from src.core.conflicts import TimeSlotOverlapCalculator, build_conflict_matrix
from src.database.models import (
    Course,
    Faculty,
    Room,
    Section,
    SectionMeeting,
    StudentDemand,
)

logger = logging.getLogger(__name__)


class RecommendationType(str, Enum):
    UNSCHEDULED_COURSE = "UNSCHEDULED_COURSE"
    INSUFFICIENT_SECTIONS = "INSUFFICIENT_SECTIONS"
    LOW_DEMAND = "LOW_DEMAND"
    TIME_CONFLICT = "TIME_CONFLICT"
    ROOM_CAPACITY = "ROOM_CAPACITY"
    FACULTY_OVERLOAD = "FACULTY_OVERLOAD"


@dataclass
class Recommendation:
    type: RecommendationType
    priority: float
    course_code: str | None
    section_crn: str | None
    description: str
    suggested_action: str


class ScheduleAnalyzer:
    def __init__(self, session: Session):
        self.session = session

    def analyze(
        self, schedule_id: int, conflict_threshold: float = 50.0
    ) -> list[Recommendation]:
        recommendations: list[Recommendation] = []

        # Load all demands
        demands = list(self.session.exec(select(StudentDemand)).all())
        if not demands:
            return []

        # Group demands by course
        demands_by_course: dict[str, list[StudentDemand]] = defaultdict(list)
        for d in demands:
            demands_by_course[d.course_code].append(d)

        # Load sections for this schedule
        sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == schedule_id)
            ).all()
        )
        sections_by_course: dict[str, list[Section]] = defaultdict(list)
        for s in sections:
            sections_by_course[s.course_code].append(s)

        # Load lookup data
        courses = {c.code: c for c in self.session.exec(select(Course)).all()}
        rooms = {r.id: r for r in self.session.exec(select(Room)).all()}
        faculty = {f.id: f for f in self.session.exec(select(Faculty)).all()}

        # 1. UNSCHEDULED_COURSE
        for course_code, course_demands in demands_by_course.items():
            if course_code not in sections_by_course:
                total_urgency = sum(d.urgency_score for d in course_demands)
                recommendations.append(Recommendation(
                    type=RecommendationType.UNSCHEDULED_COURSE,
                    priority=total_urgency,
                    course_code=course_code,
                    section_crn=None,
                    description=(
                        f"{course_code} has {len(course_demands)} demands "
                        f"but no scheduled sections"
                    ),
                    suggested_action="Add at least one section for this course",
                ))

        # 2. INSUFFICIENT_SECTIONS
        for course_code, course_sections in sections_by_course.items():
            if course_code not in demands_by_course:
                continue
            course_demands = demands_by_course[course_code]
            total_capacity = sum(s.capacity for s in course_sections)
            demand_count = len(course_demands)
            if demand_count > total_capacity:
                overflow = demand_count - total_capacity
                sorted_demands = sorted(
                    course_demands, key=lambda d: d.urgency_score, reverse=True
                )
                overflow_urgency = sum(
                    d.urgency_score for d in sorted_demands[:overflow]
                )
                recommendations.append(Recommendation(
                    type=RecommendationType.INSUFFICIENT_SECTIONS,
                    priority=overflow_urgency,
                    course_code=course_code,
                    section_crn=None,
                    description=(
                        f"{course_code}: {demand_count} demands exceed "
                        f"total capacity of {total_capacity}"
                    ),
                    suggested_action=(
                        f"Add sections or increase capacity "
                        f"(need {overflow} more seats)"
                    ),
                ))

        # 3. LOW_DEMAND
        for course_code, course_sections in sections_by_course.items():
            course_demands = demands_by_course.get(course_code, [])
            total_capacity = sum(s.capacity for s in course_sections)
            demand_count = len(course_demands)
            if total_capacity > 0 and demand_count / total_capacity < 0.3:
                recommendations.append(Recommendation(
                    type=RecommendationType.LOW_DEMAND,
                    priority=float(demand_count),
                    course_code=course_code,
                    section_crn=None,
                    description=(
                        f"{course_code}: only {demand_count} demands for "
                        f"{total_capacity} seats "
                        f"({demand_count / total_capacity * 100:.0f}% utilization)"
                    ),
                    suggested_action="Consider reducing sections or capacity",
                ))

        # 4. TIME_CONFLICT
        try:
            cm = build_conflict_matrix(self.session)
            high_pairs = cm.get_high_conflict_pairs(conflict_threshold)
            overlap_calc = TimeSlotOverlapCalculator(self.session)
            overlap_calc.compute_overlaps()

            # Prefetch meeting-0 slots once for all sections in the schedule
            # (this loop used to re-query the same meeting per conflict pair).
            all_section_ids = [
                s.id
                for course_sections in sections_by_course.values()
                for s in course_sections
                if s.id is not None
            ]
            m0_slot_by_section: dict[int, int] = {}
            if all_section_ids:
                for sm in self.session.exec(
                    select(SectionMeeting).where(
                        SectionMeeting.section_id.in_(all_section_ids),
                        SectionMeeting.meeting_sequence == 0,
                    )
                ).all():
                    m0_slot_by_section[sm.section_id] = sm.time_slot_id

            for c1, c2, score in high_pairs:
                sections_c1 = sections_by_course.get(c1, [])
                sections_c2 = sections_by_course.get(c2, [])
                if not sections_c1 or not sections_c2:
                    continue

                # Check if any sections of these two courses overlap
                has_overlap = False
                for s1 in sections_c1:
                    slot1 = m0_slot_by_section.get(s1.id)
                    if slot1 is None:
                        continue
                    overlapping = overlap_calc.get_overlapping_slots(slot1)
                    for s2 in sections_c2:
                        slot2 = m0_slot_by_section.get(s2.id)
                        if slot2 is None:
                            continue
                        if slot2 == slot1 or slot2 in overlapping:
                            has_overlap = True
                            break
                    if has_overlap:
                        break

                if has_overlap:
                    recommendations.append(Recommendation(
                        type=RecommendationType.TIME_CONFLICT,
                        priority=score,
                        course_code=c1,
                        section_crn=None,
                        description=(
                            f"{c1} and {c2} have conflict score {score:.0f} "
                            f"and overlapping sections"
                        ),
                        suggested_action=(
                            f"Move {c1} or {c2} to a non-overlapping time slot"
                        ),
                    ))
        except Exception:
            logger.warning("Could not compute time conflicts", exc_info=True)

        # 5. ROOM_CAPACITY
        for course_code, course_sections in sections_by_course.items():
            course_demands = demands_by_course.get(course_code, [])
            demand_count = len(course_demands)
            num_sections = len(course_sections)
            if num_sections == 0 or demand_count == 0:
                continue
            expected_per_section = demand_count / num_sections
            avg_urgency = sum(d.urgency_score for d in course_demands) / demand_count

            for s in course_sections:
                sm0 = self.session.exec(
                    select(SectionMeeting)
                    .where(SectionMeeting.section_id == s.id, SectionMeeting.meeting_sequence == 0)
                ).first()
                if sm0:
                    room = rooms.get(sm0.room_id)
                    if room and expected_per_section > room.capacity:
                        overflow = int(expected_per_section - room.capacity)
                        recommendations.append(Recommendation(
                            type=RecommendationType.ROOM_CAPACITY,
                            priority=overflow * avg_urgency,
                            course_code=course_code,
                            section_crn=s.crn,
                            description=(
                                f"{course_code} section {s.crn}: expected "
                                f"{expected_per_section:.0f} students but room "
                                f"{room.building} {room.number} holds {room.capacity}"
                            ),
                            suggested_action="Move to a larger room",
                        ))

        # 6. FACULTY_OVERLOAD
        faculty_credits: dict[str, float] = defaultdict(float)
        faculty_sections: dict[str, list[Section]] = defaultdict(list)
        for s in sections:
            if s.faculty_id and s.course_code in courses:
                faculty_credits[s.faculty_id] += courses[s.course_code].credits
                faculty_sections[s.faculty_id].append(s)

        for fac_id, total_credits in faculty_credits.items():
            fac = faculty.get(fac_id)
            if fac and total_credits > fac.max_credits:
                over = total_credits - fac.max_credits
                recommendations.append(Recommendation(
                    type=RecommendationType.FACULTY_OVERLOAD,
                    priority=over,
                    course_code=None,
                    section_crn=None,
                    description=(
                        f"Faculty {fac.name} ({fac_id}): assigned {total_credits} "
                        f"credits, max is {fac.max_credits}"
                    ),
                    suggested_action=(
                        f"Reassign {over:.0f} credits to another instructor"
                    ),
                ))

        # Sort by priority descending
        recommendations.sort(key=lambda r: r.priority, reverse=True)
        return recommendations


def analyze_schedule(
    session: Session, schedule_id: int, conflict_threshold: float = 50.0
) -> list[Recommendation]:
    return ScheduleAnalyzer(session).analyze(schedule_id, conflict_threshold)
