"""Absorption test (Fall 2026 back-test, experiment B).

Experiment A re-plans a term from scratch. Experiment B asks a different
question: given a FIXED, already-published timetable (section meeting
times, rooms, instructors — nothing moves), how much of the CURRENT
demand set can it actually seat? Two timetables get scored side by side —
the manually-built Banner import and a Gradimize-generated one — against
the same `StudentDemand` rows (derived from real registrations, freshmen
included), so the comparison isolates "how good is this layout of seats"
from "how good is the demand".

Seating mechanism: a deterministic, urgency-descending greedy pass (not a
CP-SAT re-solve). The timetable is fixed already — the only free variable
is which student sits in which seat, and that's the same "who gets the
seat" problem `src/core/seatswap.py` solves for its move candidates, minus
the movement. A greedy pass is simpler, fast, and produces the same
answer on every run (deterministic tie-breaks), which matters because we
score two schedules and want the only difference to be the timetable, not
solver noise. It never writes to the database: everything below reads
`Section` / `SectionMeeting` / `StudentDemand` / `Student` rows and
computes purely in memory, so the stored schedule, its `SectionEnrollment`
rows, and the demand set are all left untouched — callers do not need a
scratch DB copy for this (there is nothing to protect against; nothing is
written).

Cross-listed sections: sections whose courses share a Course.crosslist_group
are pooled into one shared seat counter (capped by the shared room when
known) so a cross-listed pair is counted as one physical class, not two —
otherwise the manual baseline's seat counts are inflated for exactly the
courses most likely to be cross-listed. See the pooling block below.

Sections without meetings: the Banner import may contain sections whose
meeting time didn't match any `TimeSlot` at import time, so they have zero
`SectionMeeting` rows. Rule (documented, applied identically to every
schedule scored): such a section is still SEATABLE (its capacity counts)
but its conflict status is UNKNOWN — we can't tell if it overlaps anything,
so we never treat it as colliding with (or being collided into by) any
other section. These sections are counted separately as
`unplaced_sections` so the caller can see how much of the "seatable"
figure rests on unverified conflict data.
"""

from collections import defaultdict

from sqlmodel import Session, select

from src.core.timeutil import slots_overlap
from src.database.models import (
    ClassStanding,
    Course,
    Room,
    ScheduleVersion,
    Section,
    SectionMeeting,
    Student,
    StudentDemand,
    TimeSlot,
)


def absorption_score(
    session: Session,
    schedule_id: int,
    top_unmet: int = 10,
) -> dict:
    """Seat the current demand set into schedule `schedule_id`'s fixed sections.

    Read-only: never mutates `Section`, `SectionEnrollment`, or
    `StudentDemand` rows. Returns a dict of comparable metrics (see module
    docstring for the seating rule and the unplaced-section convention).
    """
    schedule = session.get(ScheduleVersion, schedule_id)
    if schedule is None:
        raise ValueError(f"Schedule {schedule_id} not found")

    sections = list(
        session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
    )
    section_ids = [s.id for s in sections if s.id is not None]

    meetings_by_section: dict[int, list[SectionMeeting]] = defaultdict(list)
    if section_ids:
        for m in session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id.in_(section_ids))  # type: ignore[union-attr]
        ).all():
            meetings_by_section[m.section_id].append(m)
    for lst in meetings_by_section.values():
        lst.sort(key=lambda m: m.meeting_sequence)
    unplaced_sections = sum(1 for sid in section_ids if sid not in meetings_by_section)

    slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}

    def section_slots(section_id: int) -> list[TimeSlot]:
        return [
            slots[m.time_slot_id]
            for m in meetings_by_section.get(section_id, [])
            if m.time_slot_id in slots
        ]

    sections_by_course: dict[str, list[Section]] = defaultdict(list)
    for s in sections:
        sections_by_course[s.course_code].append(s)
    for lst in sections_by_course.values():
        lst.sort(key=lambda s: s.id or 0)

    # ── Cross-list pooling ────────────────────────────────────────────
    # Cross-listed sections (e.g. HIST210/AFAM210 under one XLST group) are
    # ONE physical class delivered under several CRNs, but Banner gives each
    # CRN its own CAPACITY — summing per-section seats double-counts the
    # room. Sections whose courses share a crosslist_group form a "pack"
    # with one shared seat counter, capped at the summed capacities or —
    # when every pack member's primary meeting sits in the same known room —
    # that room's capacity, whichever is smaller. Applied identically to
    # every schedule scored.
    group_by_course = {
        c.code: c.crosslist_group
        for c in session.exec(select(Course)).all()
        if c.crosslist_group
    }
    room_caps = {r.id: r.capacity for r in session.exec(select(Room)).all()}

    pack_of_section: dict[int, str] = {}
    pack_members: dict[str, list[Section]] = defaultdict(list)
    for s in sections:
        group = group_by_course.get(s.course_code)
        if group and s.id is not None:
            pack_of_section[s.id] = group
            pack_members[group].append(s)
    for group in [g for g, members in pack_members.items() if len(members) < 2]:
        for s in pack_members[group]:  # a pack of one is a normal section
            pack_of_section.pop(s.id, None)
        del pack_members[group]

    pack_caps: dict[str, int] = {}
    for group, members in pack_members.items():
        cap = sum(m.capacity for m in members)
        primary_rooms = {
            meetings_by_section[m.id][0].room_id
            for m in members
            if meetings_by_section.get(m.id)
        }
        if len(primary_rooms) == 1 and all(
            meetings_by_section.get(m.id) for m in members
        ):
            room_id = next(iter(primary_rooms))
            if room_id in room_caps:
                cap = min(cap, room_caps[room_id])
        pack_caps[group] = cap
    pack_free = dict(pack_caps)

    students = {s.id: s for s in session.exec(select(Student)).all()}

    demands = list(session.exec(select(StudentDemand)).all())

    # Deterministic priority: graduation-critical first, then urgency
    # score descending, then a stable tie-break so re-runs match exactly.
    demands.sort(
        key=lambda d: (
            not d.is_graduation_critical,
            -d.urgency_score,
            d.student_id,
            d.course_code,
            d.id or 0,
        )
    )

    seats_free = {s.id: s.capacity for s in sections if s.id is not None}
    occupied_slots: dict[str, list[TimeSlot]] = defaultdict(list)
    seated_course_section: dict[tuple[str, str], int] = {}
    unmet_by_course: dict[str, int] = defaultdict(int)
    seated_flags: list[bool] = []

    for d in demands:
        key = (d.student_id, d.course_code)
        if key in seated_course_section:
            # Duplicate demand row for a course already seated this pass:
            # the student already has a seat, don't consume a second one.
            seated_flags.append(True)
            continue

        candidates = sections_by_course.get(d.course_code, [])
        placed_section = None
        for cand in candidates:
            if cand.id is None:
                continue
            pack = pack_of_section.get(cand.id)
            free = pack_free[pack] if pack else seats_free.get(cand.id, 0)
            if free <= 0:
                continue
            cand_slots = section_slots(cand.id)
            if cand_slots and any(
                slots_overlap(cs, os)
                for cs in cand_slots
                for os in occupied_slots[d.student_id]
            ):
                continue
            placed_section = cand
            break

        if placed_section is None or placed_section.id is None:
            unmet_by_course[d.course_code] += 1
            seated_flags.append(False)
            continue

        placed_pack = pack_of_section.get(placed_section.id)
        if placed_pack:
            pack_free[placed_pack] -= 1
        else:
            seats_free[placed_section.id] -= 1
        occupied_slots[d.student_id].extend(section_slots(placed_section.id))
        seated_course_section[key] = placed_section.id
        seated_flags.append(True)

    total_demand = len(demands)
    seated = sum(seated_flags)

    grad_idx = [i for i, d in enumerate(demands) if d.is_graduation_critical]
    grad_critical_total = len(grad_idx)
    grad_critical_seated = sum(1 for i in grad_idx if seated_flags[i])

    fr_idx = [
        i
        for i, d in enumerate(demands)
        if (st := students.get(d.student_id)) is not None
        and st.class_standing == ClassStanding.FR
    ]
    fr_demand_total = len(fr_idx)
    fr_seated = sum(1 for i in fr_idx if seated_flags[i])

    student_total: dict[str, int] = defaultdict(int)
    student_seated: dict[str, int] = defaultdict(int)
    for d, ok in zip(demands, seated_flags):
        student_total[d.student_id] += 1
        if ok:
            student_seated[d.student_id] += 1
    distinct_students_total = len(student_total)
    distinct_students_fully_seated = sum(
        1 for sid, n in student_total.items() if student_seated[sid] == n
    )

    top_unmet_courses = [
        {"course_code": code, "unmet": count}
        for code, count in sorted(
            unmet_by_course.items(), key=lambda kv: (-kv[1], kv[0])
        )[:top_unmet]
    ]

    total_seats_offered = sum(
        s.capacity for s in sections if s.id not in pack_of_section
    ) + sum(pack_caps.values())

    def pct(n: int, d: int) -> float:
        return round(100.0 * n / d, 1) if d else 0.0

    return {
        "schedule_id": schedule_id,
        "schedule_name": schedule.name,
        "total_demand": total_demand,
        "seated": seated,
        "seated_pct": pct(seated, total_demand),
        "grad_critical_total": grad_critical_total,
        "grad_critical_seated": grad_critical_seated,
        "grad_critical_pct": pct(grad_critical_seated, grad_critical_total),
        "fr_demand_total": fr_demand_total,
        "fr_seated": fr_seated,
        "fr_pct": pct(fr_seated, fr_demand_total),
        "distinct_students_total": distinct_students_total,
        "distinct_students_fully_seated": distinct_students_fully_seated,
        "fully_seated_pct": pct(
            distinct_students_fully_seated, distinct_students_total
        ),
        "total_seats_offered": total_seats_offered,
        "crosslist_packs": len(pack_members),
        "unplaced_sections": unplaced_sections,
        "top_unmet_courses": top_unmet_courses,
    }


def compare_absorption(
    session: Session,
    schedule_ids: list[int],
    top_unmet: int = 10,
) -> list[dict]:
    """Score each schedule independently (identical demand set, identical rule)."""
    return [absorption_score(session, sid, top_unmet=top_unmet) for sid in schedule_ids]


_ROWS: list[tuple[str, str]] = [
    ("Total demand", "total_demand"),
    ("Seated", "seated"),
    ("Seated %", "seated_pct"),
    ("Grad-critical demand", "grad_critical_total"),
    ("Grad-critical seated", "grad_critical_seated"),
    ("Grad-critical seated %", "grad_critical_pct"),
    ("FR-standing demand", "fr_demand_total"),
    ("FR-standing seated", "fr_seated"),
    ("FR-standing seated %", "fr_pct"),
    ("Distinct students w/ demand", "distinct_students_total"),
    ("Distinct students fully seated", "distinct_students_fully_seated"),
    ("Fully seated %", "fully_seated_pct"),
    ("Total seats offered", "total_seats_offered"),
    ("Cross-listed packs (capacity pooled)", "crosslist_packs"),
    ("Unplaced sections (no meetings)", "unplaced_sections"),
]


def render_absorption_text(rows: list[dict]) -> str:
    """Console side-by-side table."""
    headers = [f"{r['schedule_name']} (#{r['schedule_id']})" for r in rows]
    col_w = max([len(h) for h in headers] + [24]) + 2
    label_w = max(len(label) for label, _ in _ROWS) + 2
    lines = [
        "Absorption test — current demand seated into each fixed timetable",
        "",
        "".ljust(label_w) + "".join(h.ljust(col_w) for h in headers),
    ]
    for label, key in _ROWS:
        cells = "".join(str(r[key]).ljust(col_w) for r in rows)
        lines.append(label.ljust(label_w) + cells)
    lines.append("")
    for r in rows:
        lines.append(f"Top unmet courses — {r['schedule_name']} (#{r['schedule_id']}):")
        if not r["top_unmet_courses"]:
            lines.append("  (none)")
        for u in r["top_unmet_courses"]:
            lines.append(f"  {u['course_code']}: {u['unmet']} unmet")
        lines.append("")
    return "\n".join(lines)


def render_absorption_markdown(rows: list[dict]) -> str:
    """Markdown report matching the compare_profiles/retrospective report style."""
    headers = [f"{r['schedule_name']} (#{r['schedule_id']})" for r in rows]
    lines = [
        "# Absorption test — experiment B",
        "",
        "The current demand set (real registrations, freshmen included) seated,",
        "one urgency-ordered greedy pass, into each schedule's EXISTING, FIXED",
        "sections — no section is moved, added, or removed. Same demand, same",
        "seating rule, every column; differences are attributable to the layout",
        "of seats, not the method.",
        "",
        "## Results",
        "",
        "| Metric | " + " | ".join(headers) + " |",
        "|---" * (len(rows) + 1) + "|",
    ]
    for label, key in _ROWS:
        cells = " | ".join(str(r[key]) for r in rows)
        lines.append(f"| {label} | {cells} |")
    lines += ["", "## Top unmet courses", ""]
    for r in rows:
        lines.append(f"### {r['schedule_name']} (#{r['schedule_id']})")
        lines.append("")
        if not r["top_unmet_courses"]:
            lines.append("- (none)")
        for u in r["top_unmet_courses"]:
            lines.append(f"- {u['course_code']}: {u['unmet']} unmet")
        lines.append("")
    lines += [
        "_Sections imported without a matched meeting time (`unplaced_sections`)",
        "are seatable but their conflict status is unknown — they never block, and",
        "are never blocked by, another section's time. Treat the seated % on rows",
        "with nonzero unplaced sections as an upper bound._",
        "",
    ]
    return "\n".join(lines)
