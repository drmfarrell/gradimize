"""Compare two schedule versions — "what did the re-solve actually change?".

Keyed by (course_code, section_number); reports sections added/removed and,
for sections present in both, field-level changes to time slot, room, and
instructor (meeting 0). Shared by the API diff endpoint and repair mode
(run_repair reports this diff as its deliverable: what moved).
"""

from sqlmodel import Session, select

from src.database.models import (
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionMeeting,
    TimeSlot,
)


def diff_schedules(session: Session, schedule_id: int, base_id: int) -> dict:
    """Diff `schedule_id` against `base_id` (the older/base schedule).

    Returns {"schedule", "base", "added", "removed", "changed",
    "unchanged_count"}. Raises LookupError if either schedule is missing.
    """
    schedules = {}
    for sid in (schedule_id, base_id):
        sv = session.get(ScheduleVersion, sid)
        if not sv:
            raise LookupError(f"Schedule {sid} not found")
        schedules[sid] = sv

    time_slots = {t.id: t.name for t in session.exec(select(TimeSlot)).all()}
    rooms = {
        r.id: f"{r.building} {r.number}" for r in session.exec(select(Room)).all()
    }
    faculty = {f.id: f.name for f in session.exec(select(Faculty)).all()}

    def snapshot(sid: int) -> dict[tuple[str, str], dict]:
        sections = session.exec(
            select(Section).where(Section.schedule_id == sid)
        ).all()
        ids = [x.id for x in sections if x.id is not None]
        m0 = {}
        if ids:
            for sm in session.exec(
                select(SectionMeeting).where(
                    SectionMeeting.section_id.in_(ids),  # type: ignore[union-attr]
                    SectionMeeting.meeting_sequence == 0,
                )
            ).all():
                m0[sm.section_id] = sm
        out = {}
        for sec in sections:
            sm = m0.get(sec.id)
            out[(sec.course_code, sec.section_number)] = {
                "crn": sec.crn,
                "slot": time_slots.get(sm.time_slot_id) if sm else None,
                "room": rooms.get(sm.room_id, sm.room_id) if sm else None,
                "instructor": faculty.get(sec.faculty_id) if sec.faculty_id else None,
                "pinned": sec.is_pinned,
            }
        return out

    base, target = snapshot(base_id), snapshot(schedule_id)

    added = [
        {"course_code": c, "section_number": n, **target[(c, n)]}
        for (c, n) in sorted(target.keys() - base.keys())
    ]
    removed = [
        {"course_code": c, "section_number": n, **base[(c, n)]}
        for (c, n) in sorted(base.keys() - target.keys())
    ]
    changed = []
    unchanged = 0
    for key in sorted(base.keys() & target.keys()):
        b, t = base[key], target[key]
        deltas = {
            field: {"from": b[field], "to": t[field]}
            for field in ("slot", "room", "instructor")
            if b[field] != t[field]
        }
        if deltas:
            changed.append({
                "course_code": key[0], "section_number": key[1],
                "pinned": t["pinned"], "changes": deltas,
            })
        else:
            unchanged += 1

    return {
        "schedule": {"id": schedule_id, "name": schedules[schedule_id].name},
        "base": {"id": base_id, "name": schedules[base_id].name},
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged,
    }
