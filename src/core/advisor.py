"""Advisor mode: per-student schedule sheets and counterfactual move analysis.

Two advising-desk questions, answered from a stored schedule without
touching the solver:

- `student_sheet` — a printable markdown handout of one student's week
  (course table + day-by-day agenda), for advising appointments.
- `explain_move` — "why CAN'T BIOL301-001 meet at 10am?": a static
  what-breaks analysis of moving one section to a proposed time slot,
  naming hard blockers (slot type, blocked periods, instructor
  availability/teaching conflicts, no free room) and soft costs (enrolled
  students whose other classes collide, graduation-critical seats at risk).
  It reasons against the published schedule as-is — the honest answer to
  "what would break TODAY", not a re-solve of the whole term.
- `explain_section` — the reasons behind a section's current placement
  (pin status, room-type/slot constraints, instructor qualification,
  seats, cross-listing, and whether high-overlap partner courses were
  kept in different slots). A trust feature for the section dialog.
"""

import logging
from collections import defaultdict

from sqlalchemy import func
from sqlmodel import Session, select

from src.core.conflicts import TimeSlotOverlapCalculator, get_conflict_matrix_cached
from src.core.engine import SchedulerConfig, compute_blocked_slot_ids
from src.database.models import (
    Course,
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    Student,
    StudentDemand,
    TimeSlot,
)

logger = logging.getLogger(__name__)

DAY_ORDER = ["M", "T", "W", "R", "F", "S", "U"]
DAY_NAMES = {
    "M": "Monday", "T": "Tuesday", "W": "Wednesday", "R": "Thursday",
    "F": "Friday", "S": "Saturday", "U": "Sunday",
}


def _fmt_days(days: list[str]) -> str:
    return "".join(d for d in DAY_ORDER if d in days)


def student_sheet(session: Session, schedule_id: int, student_id: str) -> str:
    """Render one student's schedule as a printable markdown sheet."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise ValueError(f"Schedule {schedule_id} not found")
    student = session.get(Student, student_id)
    if not student:
        raise ValueError(f"Student {student_id} not found")

    sections = list(session.exec(
        select(Section)
        .join(SectionEnrollment, SectionEnrollment.section_id == Section.id)
        .where(
            Section.schedule_id == schedule_id,
            SectionEnrollment.student_id == student_id,
        )
    ).all())

    lines = [
        f"# Course Schedule — {student.name} ({student.id})",
        "",
        f"**{schedule.semester} · {schedule.name}**  ",
        f"{student.major} · {student.class_standing.value} · "
        f"{student.credits_earned} credits earned",
        "",
    ]

    if not sections:
        lines.append("_No enrollments in this schedule._")
        return "\n".join(lines) + "\n"

    courses = {
        c.code: c
        for c in session.exec(
            select(Course).where(
                Course.code.in_({s.course_code for s in sections})  # type: ignore[union-attr]
            )
        ).all()
    }
    faculty = {f.id: f for f in session.exec(select(Faculty)).all()}
    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
    rooms = {r.id: r for r in session.exec(select(Room)).all()}
    meetings_by_sid: dict[int, list[SectionMeeting]] = defaultdict(list)
    for m in session.exec(
        select(SectionMeeting)
        .where(SectionMeeting.section_id.in_([s.id for s in sections]))  # type: ignore[union-attr]
        .order_by(SectionMeeting.meeting_sequence)  # type: ignore[arg-type]
    ).all():
        meetings_by_sid[m.section_id].append(m)

    lines += [
        "| Course | Title | Section | CRN | Cr | Days | Time | Room | Instructor |",
        "|--------|-------|---------|-----|----|------|------|------|------------|",
    ]
    total_credits = 0
    # (day, start, end, label) entries for the week agenda
    agenda: list[tuple[str, str, str, str]] = []
    for s in sorted(sections, key=lambda s: s.course_code):
        course = courses.get(s.course_code)
        total_credits += course.credits if course else 0
        fac = faculty.get(s.faculty_id) if s.faculty_id else None
        parts_days, parts_time, parts_room = [], [], []
        for m in meetings_by_sid.get(s.id or -1, []):
            slot = slots.get(m.time_slot_id)
            room = rooms.get(m.room_id)
            if not slot:
                continue
            parts_days.append(_fmt_days(slot.days))
            parts_time.append(f"{slot.start_time}–{slot.end_time}")
            parts_room.append(f"{room.building} {room.number}" if room else m.room_id)
            where = f"{room.building} {room.number}" if room else m.room_id
            for d in slot.days:
                agenda.append((
                    d, slot.start_time, slot.end_time,
                    f"{s.course_code}-{s.section_number} ({where})",
                ))
        lines.append(
            f"| {s.course_code} | {course.title if course else ''} "
            f"| {s.section_number} | {s.crn} "
            f"| {course.credits if course else '?'} "
            f"| {' / '.join(parts_days) or '—'} | {' / '.join(parts_time) or '—'} "
            f"| {' / '.join(parts_room) or '—'} "
            f"| {fac.name if fac else 'TBA'} |"
        )
    lines += ["", f"**Total credits: {total_credits}**", "", "## Week at a glance", ""]

    by_day: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for d, start, end, label in agenda:
        by_day[d].append((start, end, label))
    for d in DAY_ORDER:
        if d not in by_day:
            continue
        lines.append(f"**{DAY_NAMES[d]}**")
        for start, end, label in sorted(by_day[d]):
            lines.append(f"- {start}–{end} {label}")
        lines.append("")

    lines += [
        "---",
        "_Phone calendar: your advisor can export this schedule as a "
        f"calendar file (`/api/schedule/{schedule_id}/ical/student/{student_id}`)._",
    ]
    return "\n".join(lines) + "\n"


def explain_move(
    session: Session, schedule_id: int, section_id: int, target_slot_id: int
) -> dict:
    """What breaks if this section moved to the target slot — today?

    Static counterfactual against the stored schedule: no solver run, no
    cascading re-optimization. Blockers are hard rule violations; the
    student-conflict numbers are the soft price of the move.
    """
    section = session.get(Section, section_id)
    if not section or section.schedule_id != schedule_id:
        raise ValueError("Section not found in this schedule")
    target = session.get(TimeSlot, target_slot_id)
    if not target:
        raise ValueError(f"Time slot {target_slot_id} not found")
    course = session.get(Course, section.course_code)

    m0 = session.exec(
        select(SectionMeeting).where(
            SectionMeeting.section_id == section_id,
            SectionMeeting.meeting_sequence == 0,
        )
    ).first()
    current_slot = session.get(TimeSlot, m0.time_slot_id) if m0 else None

    overlaps = TimeSlotOverlapCalculator(session).compute_overlaps()
    # A slot always conflicts with itself; the overlap map stores only pairs.
    clashing = overlaps.get(target_slot_id, set()) | {target_slot_id}

    blockers: list[str] = []
    warnings: list[str] = []

    # ── Structural: slot type + blocked periods ──
    if course:
        allowed = (
            course.meeting_patterns[0].get("slot_types", [])
            if course.meeting_patterns else course.valid_slot_types
        )
        if allowed and target.slot_type not in allowed:
            blockers.append(
                f"{course.code} may only meet in slot types "
                f"{', '.join(allowed)} — {target.name} is {target.slot_type}."
            )
    config = SchedulerConfig.load_default()
    all_slots = list(session.exec(select(TimeSlot)).all())
    if target_slot_id in compute_blocked_slot_ids(all_slots, config.blocked_periods):
        names = ", ".join(bp.name for bp in config.blocked_periods)
        blockers.append(
            f"{target.name} falls in a blocked period ({names}) — no classes "
            f"may be scheduled there."
        )

    # ── Occupancy of the rest of the schedule at the target time ──
    peers = list(session.exec(
        select(Section).where(
            Section.schedule_id == schedule_id, Section.id != section_id
        )
    ).all())
    peer_ids = [p.id for p in peers if p.id is not None]
    peer_by_id = {p.id: p for p in peers}
    meetings_at_target: list[SectionMeeting] = []
    if peer_ids:
        meetings_at_target = [
            m for m in session.exec(
                select(SectionMeeting).where(
                    SectionMeeting.section_id.in_(peer_ids)  # type: ignore[union-attr]
                )
            ).all()
            if m.time_slot_id in clashing
        ]

    # ── Instructor ──
    fac = session.get(Faculty, section.faculty_id) if section.faculty_id else None
    if fac:
        if target_slot_id in (fac.unavailable_slots or []):
            blockers.append(
                f"{fac.name} is unavailable at {target.name} (their listed "
                f"availability excludes this slot)."
            )
        their_sections = {
            p.id for p in peers if p.faculty_id == fac.id and p.id is not None
        }
        for m in meetings_at_target:
            if m.section_id in their_sections:
                p = peer_by_id[m.section_id]
                blockers.append(
                    f"{fac.name} already teaches "
                    f"{p.course_code}-{p.section_number} at that time."
                )
                break

    # ── Rooms: any compatible room free at the target time? ──
    n_enrolled = len(list(session.exec(
        select(SectionEnrollment).where(SectionEnrollment.section_id == section_id)
    ).all()))
    free_rooms: list[str] = []
    if course:
        occupied = {m.room_id for m in meetings_at_target}
        compatible = [
            r for r in session.exec(
                select(Room).where(Room.is_active == True)  # noqa: E712
            ).all()
            if r.room_type.value in course.required_room_types
        ]
        free_rooms = sorted(
            r.id for r in compatible
            if r.id not in occupied and r.capacity >= n_enrolled
        )
        if compatible and not free_rooms:
            blockers.append(
                f"All {len(compatible)} {'/'.join(course.required_room_types)} "
                f"rooms are occupied (or too small for {n_enrolled} students) "
                f"at {target.name}."
            )

    # ── Enrolled students: whose other classes collide? ──
    roster = [
        e.student_id for e in session.exec(
            select(SectionEnrollment).where(
                SectionEnrollment.section_id == section_id
            )
        ).all()
    ]
    conflicted: set[str] = set()
    if roster and meetings_at_target:
        clash_section_ids = {m.section_id for m in meetings_at_target}
        for e in session.exec(
            select(SectionEnrollment).where(
                SectionEnrollment.student_id.in_(roster),  # type: ignore[union-attr]
                SectionEnrollment.section_id.in_(clash_section_ids),  # type: ignore[union-attr]
            )
        ).all():
            conflicted.add(e.student_id)
    grad_critical = 0
    if conflicted:
        grad_critical = len(list(session.exec(
            select(StudentDemand).where(
                StudentDemand.student_id.in_(conflicted),  # type: ignore[union-attr]
                StudentDemand.course_code == section.course_code,
                StudentDemand.is_graduation_critical == True,  # noqa: E712
            )
        ).all()))
    if conflicted:
        warnings.append(
            f"{len(conflicted)} of {len(roster)} enrolled students have "
            f"another class at that time"
            + (f" ({grad_critical} graduation-critical)" if grad_critical else "")
            + "."
        )

    if course and course.crosslist_group:
        warnings.append(
            f"{course.code} is cross-listed (group {course.crosslist_group}) — "
            f"its partner sections meet as the same class and would move too."
        )

    if blockers:
        verdict = "Cannot move: " + blockers[0]
        if len(blockers) > 1:
            verdict += f" (+{len(blockers) - 1} more blocker(s))"
    elif conflicted:
        verdict = (
            f"Possible, at a price: {len(conflicted)} student conflict(s)"
            + (f" including {grad_critical} graduation-critical" if grad_critical else "")
            + (f"; room options: {', '.join(free_rooms[:3])}" if free_rooms else "")
            + "."
        )
    else:
        verdict = (
            "Freely movable: no blockers and no student conflicts"
            + (f"; room options: {', '.join(free_rooms[:3])}" if free_rooms else "")
            + "."
        )

    return {
        "section": {
            "id": section_id,
            "crn": section.crn,
            "course_code": section.course_code,
            "section_number": section.section_number,
            "current_slot": current_slot.name if current_slot else None,
            "instructor": fac.name if fac else None,
        },
        "target_slot": {"id": target_slot_id, "name": target.name},
        "blockers": blockers,
        "warnings": warnings,
        "student_conflicts": {
            "count": len(conflicted),
            "graduation_critical": grad_critical,
            "students": sorted(conflicted)[:5],
        },
        "free_rooms": free_rooms,
        "verdict": verdict,
    }


def explain_section(session: Session, schedule_id: int, section_id: int) -> dict:
    """Human-readable reasons behind a section's placement (trust feature).

    Raises ValueError if the section is not in this schedule (the route
    translates that to a 404).
    """
    section = session.get(Section, section_id)
    if not section or section.schedule_id != schedule_id:
        raise ValueError("Section not found in this schedule")
    course = session.get(Course, section.course_code)
    m0 = session.exec(
        select(SectionMeeting).where(
            SectionMeeting.section_id == section_id,
            SectionMeeting.meeting_sequence == 0,
        )
    ).first()
    room = session.get(Room, m0.room_id) if m0 else None
    slot = session.get(TimeSlot, m0.time_slot_id) if m0 else None
    fac = session.get(Faculty, section.faculty_id) if section.faculty_id else None

    reasons: list[str] = []
    if section.is_pinned:
        reasons.append(
            f"📌 Pinned ({section.pin_reason or 'no reason recorded'}) — the "
            "solver must keep this placement on re-solves."
        )
    if course:
        rooms_ok = [
            r for r in session.exec(select(Room).where(Room.is_active == True)).all()  # noqa: E712
            if r.room_type.value in course.required_room_types
        ]
        reasons.append(
            f"Room must be one of type {', '.join(course.required_room_types)} — "
            f"{len(rooms_ok)} rooms qualify"
            + (f"; assigned {room.building} {room.number} "
               f"(capacity {room.capacity})" if room else "")
            + "."
        )
        if course.valid_slot_types:
            reasons.append(
                "May only meet in slot types "
                f"{', '.join(course.valid_slot_types)}"
                + (f"; assigned {slot.name}" if slot else "")
                + "."
            )
    if fac:
        n_unavail = len(fac.unavailable_slots or [])
        approved = fac.approved_courses or []
        detail = f"Taught by {fac.name}"
        if approved:
            detail += f" (one of the qualified instructors for {section.course_code})"
        if n_unavail:
            detail += f"; {n_unavail} time slots excluded by their availability"
        reasons.append(detail + ".")
    n_enrolled = session.exec(
        select(func.count()).select_from(SectionEnrollment).where(
            SectionEnrollment.section_id == section_id
        )
    ).one()
    cap_note = f"Seats {n_enrolled}/{section.capacity}"
    if room and room.capacity < section.capacity:
        cap_note += f" — enrollment capped at room capacity {room.capacity}"
    reasons.append(cap_note + ".")

    if course and course.crosslist_group:
        partners = session.exec(
            select(Course).where(
                Course.crosslist_group == course.crosslist_group,
                Course.code != course.code,
            )
        ).all()
        if partners:
            reasons.append(
                f"Cross-listed with {', '.join(sorted(p.code for p in partners))}: "
                f"one class under multiple codes — same room, time, and "
                f"instructor; the room's seats are shared across the codes."
            )

    # High-conflict partner courses: were they kept apart?
    try:
        matrix = get_conflict_matrix_cached(session)
        top = matrix.get_top_conflicts_for_course(section.course_code, n=3)
        if top and slot:
            partner_secs = session.exec(
                select(Section).where(
                    Section.schedule_id == schedule_id,
                    Section.course_code.in_([c for c, _ in top]),
                )
            ).all()
            ids = [x.id for x in partner_secs if x.id]
            partner_slots = {}
            if ids:
                for sm in session.exec(
                    select(SectionMeeting).where(
                        SectionMeeting.section_id.in_(ids),
                        SectionMeeting.meeting_sequence == 0,
                    )
                ).all():
                    partner_slots[sm.section_id] = sm.time_slot_id
            clashes = sum(
                1 for x in partner_secs if partner_slots.get(x.id) == slot.id
            )
            names = ", ".join(f"{c} ({score:.0f})" for c, score in top)
            reasons.append(
                f"Highest student-overlap courses: {names} — "
                + ("⚠ shares a slot with one of them."
                   if clashes else "kept in different slots.")
            )
    except Exception:
        logger.debug("conflict explanation unavailable", exc_info=True)

    return {"section_id": section_id, "crn": section.crn, "reasons": reasons}
