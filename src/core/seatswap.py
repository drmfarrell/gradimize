"""Pacing-aware seat assignment: stage 2 of student scheduling.

The solver (stage 1) fixes the timetable and how many seats each section
holds; WHICH student sits in WHICH sibling section is largely fungible.
This pass reassigns students among sibling sections of the same course to
improve each student's personal week — without touching the timetable,
capacities, or anyone's course list.

Preference dials (Student.pacing_preference, or a run-wide default):
- "cluster"  — commuter/worker profile: fewest days on campus, tight
  packing; gaps and extra days weigh heavily, marathons are tolerated.
- "spread"   — breathing-room profile: long back-to-back marathons and
  heavy stacked days weigh heavily, extra days are fine.
- balanced   — the schedule-quality report's badness formula as-is.

Moves are strictly safe: a student only moves into a sibling section with
a free seat whose meetings don't collide with the rest of their schedule.
Greedy best-improvement passes repeat until quiescent. Deterministic
(sorted iteration, no randomness), so re-runs are stable.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, select

from src.core.schedule_quality import StudentQuality, analyze_student
from src.core.timeutil import slots_overlap
from src.database.models import (
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    Student,
    TimeSlot,
)

logger = logging.getLogger(__name__)

MODES = ("balanced", "cluster", "spread")
IMPROVEMENT_EPSILON = 1e-6


def _mode_score(sq: StudentQuality, mode: str) -> float:
    """Lower is better. Each mode re-weights the same measured facts."""
    if mode == "cluster":
        return (
            sq.total_gap_minutes / 30
            + 90.0 * max(0, sq.days_on_campus - 2)
            + max(0.0, (sq.max_marathon_minutes - 300) / 120)
        )
    if mode == "spread":
        # Breathing room is the goal: marathons hurt a lot, gaps barely
        # count (a gap IS the breathing room this profile asked for).
        return (
            max(0.0, (sq.max_marathon_minutes - 120) / 15)
            + 2.0 * sq.heavy_days
            + sq.total_gap_minutes / 600
        )
    return sq.badness  # balanced: the quality report's own formula


@dataclass
class RebalanceResult:
    schedule_id: int
    default_mode: str
    moves: int = 0
    students_improved: int = 0
    passes: int = 0
    mean_badness_before: float = 0.0
    mean_badness_after: float = 0.0
    moved_students: list[str] = field(default_factory=list)


def rebalance_seats(
    session: Session,
    schedule_id: int,
    default_mode: str = "balanced",
    max_passes: int = 6,
) -> RebalanceResult:
    """Reassign students among sibling sections to fit their week shape.

    Mutates SectionEnrollment rows (commits once at the end). The timetable,
    section capacities, and every student's COURSE list are untouched.
    """
    if default_mode not in MODES:
        raise ValueError(f"Unknown mode {default_mode!r}; expected one of {MODES}")
    schedule = session.get(ScheduleVersion, schedule_id)
    if schedule is None:
        raise ValueError(f"Schedule {schedule_id} not found")

    sections = list(session.exec(
        select(Section).where(Section.schedule_id == schedule_id)
    ).all())
    section_by_id = {s.id: s for s in sections}
    enrollments = [
        e for e in session.exec(select(SectionEnrollment)).all()
        if e.section_id in section_by_id
    ]
    meetings_by_section: dict[int, list[SectionMeeting]] = defaultdict(list)
    for m in session.exec(select(SectionMeeting)).all():
        if m.section_id in section_by_id:
            meetings_by_section[m.section_id].append(m)
    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
    prefs = {
        s.id: s.pacing_preference
        for s in session.exec(select(Student)).all()
        if s.pacing_preference in MODES
    }

    siblings_by_course: dict[str, list[int]] = defaultdict(list)
    for s in sections:
        siblings_by_course[s.course_code].append(s.id)
    movable_courses = {c for c, ids in siblings_by_course.items() if len(ids) > 1}

    seats_free = {
        s.id: s.capacity for s in sections
    }
    enrollment_row: dict[tuple[str, int], SectionEnrollment] = {}
    student_sections: dict[str, list[int]] = defaultdict(list)
    for e in enrollments:
        seats_free[e.section_id] -= 1
        enrollment_row[e.student_id, e.section_id] = e
        student_sections[e.student_id].append(e.section_id)

    def quality(sid: str, section_ids: list[int]) -> StudentQuality:
        return analyze_student(sid, section_ids, meetings_by_section, slots)

    def collides(candidate_id: int, other_ids: list[int]) -> bool:
        cand_slots = [
            slots[m.time_slot_id]
            for m in meetings_by_section.get(candidate_id, [])
            if m.time_slot_id in slots
        ]
        for oid in other_ids:
            for m in meetings_by_section.get(oid, []):
                slot = slots.get(m.time_slot_id)
                if slot is None:
                    continue
                if any(slots_overlap(cs, slot) for cs in cand_slots):
                    return True
        return False

    result = RebalanceResult(schedule_id=schedule_id, default_mode=default_mode)
    all_students = sorted(student_sections)
    before = [quality(sid, student_sections[sid]).badness for sid in all_students]
    result.mean_badness_before = (
        round(sum(before) / len(before), 2) if before else 0.0
    )

    improved: set[str] = set()
    for _pass in range(max_passes):
        moved_this_pass = 0
        for sid in all_students:
            mode = prefs.get(sid, default_mode)
            current = student_sections[sid]
            base_score = _mode_score(quality(sid, current), mode)
            for idx, cur_sec_id in enumerate(list(current)):
                course = section_by_id[cur_sec_id].course_code
                if course not in movable_courses:
                    continue
                others = current[:idx] + current[idx + 1:]
                best = None  # (score, candidate_id)
                for cand_id in sorted(siblings_by_course[course]):
                    if cand_id == cur_sec_id or seats_free.get(cand_id, 0) <= 0:
                        continue
                    if collides(cand_id, others):
                        continue
                    trial = others[:idx] + [cand_id] + others[idx:]
                    score = _mode_score(quality(sid, trial), mode)
                    if score < base_score - IMPROVEMENT_EPSILON and (
                        best is None or score < best[0]
                    ):
                        best = (score, cand_id)
                if best is None:
                    continue
                _, cand_id = best
                row = enrollment_row.pop((sid, cur_sec_id))
                row.section_id = cand_id
                session.add(row)
                enrollment_row[sid, cand_id] = row
                seats_free[cur_sec_id] += 1
                seats_free[cand_id] -= 1
                current[idx] = cand_id
                base_score = _mode_score(quality(sid, current), mode)
                moved_this_pass += 1
                improved.add(sid)
        result.passes = _pass + 1
        result.moves += moved_this_pass
        if moved_this_pass == 0:
            break

    session.commit()
    after = [quality(sid, student_sections[sid]).badness for sid in all_students]
    result.mean_badness_after = (
        round(sum(after) / len(after), 2) if after else 0.0
    )
    result.students_improved = len(improved)
    result.moved_students = sorted(improved)[:50]
    logger.info(
        "Seat rebalance (%s): %d moves across %d passes, %d students improved, "
        "mean badness %.2f -> %.2f",
        default_mode, result.moves, result.passes, result.students_improved,
        result.mean_badness_before, result.mean_badness_after,
    )
    return result
