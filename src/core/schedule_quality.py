"""Student schedule-quality report: after a solve, how livable is each
student's week?

A schedule can satisfy every hard constraint — no room clashes, no faculty
double-books — and still hand a student a week nobody would choose: three
hours dead between a 9am class and a 3pm class, five separate trips to
campus, or a four-course marathon with no real break. This module expands
each enrolled student's meetings into a day-by-day picture and scores it.

Definitions (all thresholds are minutes unless noted):
- A TimeSlot's `days` list means the meeting recurs on each of those days;
  expanding every meeting per day it occurs on is the first step for every
  metric below.
- A gap strictly greater than 15 minutes between two consecutive classes on
  the same day is "idle time": it adds to `total_gap_minutes` (summed
  across every day it occurs — not just once) and is a candidate for
  `longest_gap_minutes` (the single biggest instance, not a sum).
- A gap strictly less than 15 minutes fuses the two classes into one
  "marathon" — a continuous on-campus stretch. `max_marathon_minutes` is
  the longest such fused stretch anywhere in the student's week (an
  isolated class with no back-to-back neighbor is a marathon of just its
  own length).
- A gap of EXACTLY 15 minutes is a wash: an ordinary passing period. It is
  not counted as idle time, but it does not fuse into a marathon either.
  This is a deliberate reading of the spec's two independent ">15" /
  "<15" thresholds rather than an invented third rule.
- `heavy_days` counts days with 3 or more class meetings, irrespective of
  the gaps between them.
- `earliest_start` / `latest_end` are the earliest class-start and latest
  class-end anywhere in the student's week (not computed per day).

Badness score (used only to rank the worst-off students; the constants are
a judgment call, not a physical unit):

    badness = total_gap_minutes / 60
            + 60 * max(0, days_on_campus - 3)
            + max(0, (max_marathon_minutes - 180) / 60)

i.e. gap hours, plus a heavy 60-point penalty per day on campus beyond
three (day-spread is the most visible quality-of-life problem for a
commuter-heavy student body, so it's weighted to dominate the ranking),
plus marathon hours beyond three.

`department_prime_time` is a separate, enrollment-independent fairness
readout: for each course-code alphabetic prefix (department), what share
of its section-meetings start in "prime time" (10:00-14:00, the slots
everyone wants)? A department stuck entirely outside that window is a
staffing/equity signal worth a look — it does not depend on who is
enrolled, only on where the schedule placed the department's sections.
"""

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, select

from src.core.timeutil import time_to_minutes
from src.database.models import (
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

GAP_THRESHOLD_MINUTES = 15  # >15 = idle gap; <15 = fused marathon; ==15 = neither
PRIME_START_MINUTES = 10 * 60  # 10:00
PRIME_END_MINUTES = 14 * 60  # 14:00, exclusive
HEAVY_DAY_MIN_CLASSES = 3
FIVE_DAY_THRESHOLD = 5  # "5-day spread" -> on campus this many days or more
BIG_GAP_MINUTES = 3 * 60  # "3h+ single gap" (inclusive)
MARATHON_OVER_MINUTES = 3 * 60  # a marathon stretch "over 3h" (exclusive)
MARATHON_OVER_COUNT_THRESHOLD = 3  # "3+ marathons over 3h"
DAY_PENALTY_BASELINE_DAYS = 3
DAY_PENALTY_PER_DAY = 60.0
WORST_N = 20

_DEPT_PREFIX_RE = re.compile(r"^[A-Za-z]+")


@dataclass
class StudentQuality:
    student_id: str
    days_on_campus: int
    total_gap_minutes: int
    longest_gap_minutes: int
    max_marathon_minutes: int
    marathons_over_3h: int
    heavy_days: int
    earliest_start: str | None
    latest_end: str | None
    badness: float = 0.0
    # Conflict taxonomy borrowed from Purdue's open-source UniTime system:
    # a day is "lunch-crunched" when classes leave no 30-minute free block
    # between 11:00 and 13:00; a "long workday" spans more than 6 hours
    # from first class start to last class end.
    lunch_crunch_days: int = 0
    longest_workday_minutes: int = 0
    long_workdays: int = 0


@dataclass
class DepartmentPrimeTime:
    department: str
    total_meetings: int
    prime_meetings: int
    prime_share: float  # prime_meetings / total_meetings, 0.0-1.0


@dataclass
class StudentQualityReport:
    schedule_id: int
    semester: str
    total_students: int
    mean_days_on_campus: float = 0.0
    median_days_on_campus: float = 0.0
    mean_gap_minutes: float = 0.0
    students_5_day: int = 0
    students_big_gap: int = 0
    students_heavy_marathon: int = 0
    students_no_lunch: int = 0
    students_long_workday: int = 0
    worst_students: list[StudentQuality] = field(default_factory=list)
    department_prime_time: list[DepartmentPrimeTime] = field(default_factory=list)


def _department_prefix(course_code: str) -> str:
    """Alphabetic prefix of a course code: "BIOL301" -> "BIOL"."""
    m = _DEPT_PREFIX_RE.match(course_code)
    return m.group(0).upper() if m else course_code


def _is_prime_time(slot: TimeSlot) -> bool:
    """10:00 <= start < 14:00."""
    start = time_to_minutes(slot.start_time)
    return PRIME_START_MINUTES <= start < PRIME_END_MINUTES


LUNCH_START_MINUTES = 11 * 60
LUNCH_END_MINUTES = 13 * 60
LUNCH_MIN_FREE_MINUTES = 30
WORKDAY_OVER_MINUTES = 6 * 60


def _lunch_crunched(intervals: list[tuple[int, int]]) -> bool:
    """True if one day's class intervals leave no 30-minute free block
    between 11:00 and 13:00 (definition per UniTime's lunch-break conflict)."""
    busy = sorted(
        (max(s, LUNCH_START_MINUTES), min(e, LUNCH_END_MINUTES))
        for s, e in intervals
        if e > LUNCH_START_MINUTES and s < LUNCH_END_MINUTES
    )
    if not busy:
        return False
    cursor = LUNCH_START_MINUTES
    max_free = 0
    for s, e in busy:
        max_free = max(max_free, s - cursor)
        cursor = max(cursor, e)
    max_free = max(max_free, LUNCH_END_MINUTES - cursor)
    return max_free < LUNCH_MIN_FREE_MINUTES


def _day_metrics(intervals: list[tuple[int, int]]) -> tuple[int, int, list[int]]:
    """One day's (start, end) minute intervals -> (gap_minutes, longest_gap,
    marathon_lengths). See the module docstring for the >15 / <15 / ==15
    gap rules."""
    ordered = sorted(intervals)
    gap_total = 0
    longest_gap = 0
    marathons: list[int] = []
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        gap = max(0, start - cur_end)
        if gap < GAP_THRESHOLD_MINUTES:
            cur_end = max(cur_end, end)
            continue
        marathons.append(cur_end - cur_start)
        if gap > GAP_THRESHOLD_MINUTES:
            gap_total += gap
            longest_gap = max(longest_gap, gap)
        cur_start, cur_end = start, end
    marathons.append(cur_end - cur_start)
    return gap_total, longest_gap, marathons


def analyze_student(
    student_id: str,
    section_ids: list[int],
    meetings_by_section: dict[int, list[SectionMeeting]],
    slots: dict[int, TimeSlot],
) -> StudentQuality:
    """Expand one student's enrolled meetings into per-day class lists and
    compute their week's metrics."""
    by_day: dict[str, list[tuple[int, int]]] = defaultdict(list)
    earliest_min: int | None = None
    earliest_str: str | None = None
    latest_min: int | None = None
    latest_str: str | None = None

    for section_id in section_ids:
        for meeting in meetings_by_section.get(section_id, []):
            slot = slots.get(meeting.time_slot_id)
            if slot is None:
                continue
            start_min = time_to_minutes(slot.start_time)
            end_min = time_to_minutes(slot.end_time)
            if earliest_min is None or start_min < earliest_min:
                earliest_min, earliest_str = start_min, slot.start_time
            if latest_min is None or end_min > latest_min:
                latest_min, latest_str = end_min, slot.end_time
            for day in slot.days:
                by_day[day].append((start_min, end_min))

    total_gap = 0
    longest_gap = 0
    max_marathon = 0
    marathons_over_3h = 0
    heavy_days = 0
    lunch_crunch_days = 0
    longest_workday = 0
    long_workdays = 0
    for intervals in by_day.values():
        if len(intervals) >= HEAVY_DAY_MIN_CLASSES:
            heavy_days += 1
        day_gap, day_longest, marathons = _day_metrics(intervals)
        total_gap += day_gap
        longest_gap = max(longest_gap, day_longest)
        for m in marathons:
            max_marathon = max(max_marathon, m)
            if m > MARATHON_OVER_MINUTES:
                marathons_over_3h += 1
        if _lunch_crunched(intervals):
            lunch_crunch_days += 1
        span = max(e for _, e in intervals) - min(s for s, _ in intervals)
        longest_workday = max(longest_workday, span)
        if span > WORKDAY_OVER_MINUTES:
            long_workdays += 1

    badness = (
        total_gap / 60
        + DAY_PENALTY_PER_DAY * max(0, len(by_day) - DAY_PENALTY_BASELINE_DAYS)
        + max(0.0, (max_marathon - MARATHON_OVER_MINUTES) / 60)
    )

    return StudentQuality(
        student_id=student_id,
        days_on_campus=len(by_day),
        total_gap_minutes=total_gap,
        longest_gap_minutes=longest_gap,
        max_marathon_minutes=max_marathon,
        marathons_over_3h=marathons_over_3h,
        heavy_days=heavy_days,
        earliest_start=earliest_str,
        latest_end=latest_str,
        badness=badness,
        lunch_crunch_days=lunch_crunch_days,
        longest_workday_minutes=longest_workday,
        long_workdays=long_workdays,
    )


def analyze_student_quality(session: Session, schedule_id: int) -> StudentQualityReport:
    """Per-student schedule-quality metrics for every enrolled student in a
    schedule, plus a department-level prime-time fairness readout.

    See the module docstring for metric definitions and the badness formula
    used to rank `worst_students` (worst first, capped at 20).
    """
    schedule = session.get(ScheduleVersion, schedule_id)
    if schedule is None:
        raise ValueError(f"Schedule {schedule_id} not found")

    sections = list(
        session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
    )
    section_ids = {s.id for s in sections}
    section_by_id = {s.id: s for s in sections}
    enrollments = [
        e for e in session.exec(select(SectionEnrollment)).all()
        if e.section_id in section_ids
    ]
    meetings = [
        m for m in session.exec(select(SectionMeeting)).all()
        if m.section_id in section_ids
    ]
    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}

    # Department prime-time: every meeting in the schedule, regardless of
    # enrollment — this is a staffing/fairness signal, not a student one.
    dept_total: dict[str, int] = defaultdict(int)
    dept_prime: dict[str, int] = defaultdict(int)
    for m in meetings:
        slot = slots.get(m.time_slot_id)
        section = section_by_id.get(m.section_id)
        if slot is None or section is None:
            continue
        dept = _department_prefix(section.course_code)
        dept_total[dept] += 1
        if _is_prime_time(slot):
            dept_prime[dept] += 1
    department_prime_time = [
        DepartmentPrimeTime(
            department=dept,
            total_meetings=total,
            prime_meetings=dept_prime.get(dept, 0),
            prime_share=dept_prime.get(dept, 0) / total if total else 0.0,
        )
        for dept, total in sorted(dept_total.items())
    ]

    student_sections: dict[str, list[int]] = defaultdict(list)
    for e in enrollments:
        student_sections[e.student_id].append(e.section_id)
    meetings_by_section: dict[int, list[SectionMeeting]] = defaultdict(list)
    for m in meetings:
        meetings_by_section[m.section_id].append(m)

    students = [
        analyze_student(student_id, sec_ids, meetings_by_section, slots)
        for student_id, sec_ids in student_sections.items()
    ]

    report = StudentQualityReport(
        schedule_id=schedule_id,
        semester=schedule.semester,
        total_students=len(students),
        department_prime_time=department_prime_time,
    )
    if students:
        report.mean_days_on_campus = statistics.mean(s.days_on_campus for s in students)
        report.median_days_on_campus = statistics.median(s.days_on_campus for s in students)
        report.mean_gap_minutes = statistics.mean(s.total_gap_minutes for s in students)
        report.students_5_day = sum(
            1 for s in students if s.days_on_campus >= FIVE_DAY_THRESHOLD
        )
        report.students_big_gap = sum(
            1 for s in students if s.longest_gap_minutes >= BIG_GAP_MINUTES
        )
        report.students_heavy_marathon = sum(
            1 for s in students if s.marathons_over_3h >= MARATHON_OVER_COUNT_THRESHOLD
        )
        report.students_no_lunch = sum(
            1 for s in students if s.lunch_crunch_days >= 1
        )
        report.students_long_workday = sum(
            1 for s in students if s.long_workdays >= 1
        )
        report.worst_students = sorted(students, key=lambda s: s.badness, reverse=True)[
            :WORST_N
        ]

    return report
