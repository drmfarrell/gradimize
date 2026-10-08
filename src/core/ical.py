"""iCal (.ics) export for Gradimize schedules.

Produces RFC 5545 VCALENDAR text for three audiences — an instructor's
teaching load, a room's booking calendar, and a student's enrolled
sections — from a solved ``ScheduleVersion``. Pure standard library: no
``icalendar`` dependency.

Floating local time, on purpose
--------------------------------
Every VEVENT uses a "floating" local DATE-TIME (``DTSTART:YYYYMMDDTHHMMSS``,
no ``TZID`` parameter, no trailing ``Z``). Gradimize has no notion of a
campus timezone in its data model, and a floating time is exactly what a
calendar app should show at 9:00 AM wherever the reader's calendar happens
to be set — which for a single-campus schedule is the only sane behavior
without inventing timezone data we don't have.

DTSTAMP is normally "now" at generation time, but this repo prefers
deterministic outputs (re-running an export should produce byte-identical
text), so DTSTAMP is derived from ``term_start`` instead of
``datetime.now()``. Same reasoning is why it is NOT suffixed with ``Z``
here: it stays consistent with the floating times used everywhere else in
the same file, even though RFC 5545 §3.8.7.2 technically calls for DTSTAMP
to be UTC. Calendar readers do not validate DTSTAMP against a clock, so
this is a harmless, deliberate deviation in exchange for reproducibility.

Approximate term dates
-----------------------
``default_term_dates()`` maps a semester code ("2026FA") to a rough
Aug25-Dec12 / Jan20-May8 / Jun1-Jul31 window purely so an .ics file has
*some* start/end to anchor its weekly RRULE and UNTIL clause. These dates
are NOT the registrar's academic calendar — they will be off by a few days
in either direction most terms. Callers that know the real term start/end
(e.g. from an academic-calendar table) should always pass ``term_start`` /
``term_end`` explicitly; the default is a calendar-convenience fallback,
not a source of truth.

Not-found behavior
-------------------
Every public function raises ``ValueError`` (with a clear message) if the
schedule, or the specific instructor/room/student, does not exist. This
matches the rest of ``src/core`` (see ``viability.py``, ``export.py``),
where the API layer already has a blanket ``except ValueError -> 404``
pattern for core-layer lookups.
"""

import logging
from datetime import date, timedelta

from sqlmodel import Session, func, select

from src.core.timeutil import parse_semester
from src.database.models import (
    Course,
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    Student,
    TimeSlot,
)

logger = logging.getLogger(__name__)

_PRODID = "-//Gradimize//Schedule Export//EN"

# TimeSlot.days uses single-letter day codes (Banner convention); RRULE
# wants two-letter BYDAY codes. The weekday map also drives "first date
# on/after term_start" and gives BYDAY a stable Mon->Sun ordering
# regardless of how `days` happens to be ordered in the database.
_DAY_TO_RRULE = {"M": "MO", "T": "TU", "W": "WE", "R": "TH", "F": "FR", "S": "SA", "U": "SU"}
_DAY_TO_WEEKDAY = {"M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6}

# (month, day) start/end pairs for default_term_dates(), keyed by the
# normalized term name that src.core.timeutil.parse_semester() returns.
_SEMESTER_WINDOWS = {
    "FALL": ((8, 25), (12, 12)),
    "SPRING": ((1, 20), (5, 8)),
    "SUMMER": ((6, 1), (7, 31)),
}


def default_term_dates(semester: str) -> tuple[date, date]:
    """Approximate (term_start, term_end) for a semester code.

    "YYYYFA" -> Aug 25-Dec 12 of that year; "YYYYSP" -> Jan 20-May 8;
    "YYYYSU" -> Jun 1-Jul 31. These are ROUGH DEFAULTS for calendar
    convenience only — not the registrar's academic calendar. Pass real
    term_start/term_end to the ics_for_* functions when you have them.

    Raises:
        ValueError: if `semester` cannot be parsed, or parses to a term
            (e.g. "WINTER") with no default window defined.
    """
    year, term = parse_semester(semester)  # raises ValueError on garbage
    window = _SEMESTER_WINDOWS.get(term)
    if window is None:
        raise ValueError(
            f"No default term dates for semester {semester!r} (term={term})"
        )
    (start_month, start_day), (end_month, end_day) = window
    return date(year, start_month, start_day), date(year, end_month, end_day)


def _escape_text(value: str) -> str:
    """Escape a TEXT value per RFC 5545 §3.3.11: backslash first (so we
    don't double-escape the backslashes this function itself inserts),
    then semicolon, comma, and embedded newlines."""
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\r\n", "\\n")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\n")
    return value


def _fold_line(line: str) -> str:
    """Fold a content line to <=75 octets per RFC 5545 §3.1.

    Continuation lines are joined by CRLF + a single leading space, and
    that leading space counts toward the continuation line's own 75-octet
    budget. Folding is done on UTF-8 bytes but never splits a multi-byte
    UTF-8 sequence (it backs off while the next byte is a continuation
    byte, 10xxxxxx).
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line

    chunks: list[bytes] = []
    start = 0
    limit = 75  # first chunk gets the full budget (no leading space yet)
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and end > start and (data[end] & 0xC0) == 0x80:
            end -= 1  # don't split a multi-byte UTF-8 sequence
        chunks.append(data[start:end])
        start = end
        limit = 74  # later chunks reserve 1 octet for the leading space
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def _first_occurrence(term_start: date, days: list[str]) -> date:
    """First date on/after term_start whose weekday is one of `days`."""
    weekdays = {_DAY_TO_WEEKDAY[d] for d in days if d in _DAY_TO_WEEKDAY}
    if not weekdays:
        return term_start
    for offset in range(7):  # a full week always contains a match
        candidate = term_start + timedelta(days=offset)
        if candidate.weekday() in weekdays:
            return candidate
    return term_start  # unreachable


def _byday_string(days: list[str]) -> str:
    ordered = sorted(
        {d for d in days if d in _DAY_TO_WEEKDAY}, key=lambda d: _DAY_TO_WEEKDAY[d]
    )
    return ",".join(_DAY_TO_RRULE[d] for d in ordered)


def _time_compact(hhmm: str) -> str:
    """"08:00" -> "080000" (TimeSlot times carry no seconds)."""
    hours, minutes = hhmm.split(":")
    return f"{int(hours):02d}{int(minutes):02d}00"


def _vevent_lines(
    schedule_id: int,
    section: Section,
    course: Course | None,
    meeting: SectionMeeting,
    slot: TimeSlot,
    room: Room | None,
    description: str,
    term_start: date,
    term_end: date,
    dtstamp: str,
) -> list[str]:
    first = _first_occurrence(term_start, slot.days)
    date_part = first.strftime("%Y%m%d")
    dtstart = f"{date_part}T{_time_compact(slot.start_time)}"
    dtend = f"{date_part}T{_time_compact(slot.end_time)}"
    byday = _byday_string(slot.days)
    until = f"{term_end.strftime('%Y%m%d')}T235959"

    title = course.title if course is not None else ""
    summary = f"{section.course_code}-{section.section_number} {title}".rstrip()
    location = f"{room.building} {room.number}" if room is not None else meeting.room_id
    uid = (
        f"gradimize-{schedule_id}-{section.crn}-m{meeting.meeting_sequence}"
        "@gradimize"
    )

    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"RRULE:FREQ=WEEKLY;BYDAY={byday};UNTIL={until}",
        f"SUMMARY:{_escape_text(summary)}",
        f"LOCATION:{_escape_text(location)}",
        f"DESCRIPTION:{_escape_text(description)}",
        "END:VEVENT",
    ]


# A calendar "entry" bundles everything one SectionMeeting needs to become
# a VEVENT. `course` and `room` may be None (dangling foreign key — FKs are
# not enforced at the DB layer and re-imports can leave stale references);
# `description` is precomputed per-audience by each ics_for_* function.
_Entry = tuple[Section, "Course | None", SectionMeeting, TimeSlot, "Room | None", str]


def _render_calendar(
    schedule_id: int,
    cal_name: str,
    entries: list[_Entry],
    term_start: date,
    term_end: date,
) -> str:
    dtstamp = f"{term_start.strftime('%Y%m%d')}T000000"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape_text(cal_name)}",
    ]
    for section, course, meeting, slot, room, description in entries:
        lines.extend(
            _vevent_lines(
                schedule_id, section, course, meeting, slot, room,
                description, term_start, term_end, dtstamp,
            )
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_line(line) for line in lines) + "\r\n"


def _get_schedule(session: Session, schedule_id: int) -> ScheduleVersion:
    schedule = session.get(ScheduleVersion, schedule_id)
    if schedule is None:
        raise ValueError(f"Schedule {schedule_id} not found")
    return schedule


def _resolve_term(
    schedule: ScheduleVersion, term_start: date | None, term_end: date | None
) -> tuple[date, date]:
    if term_start is None or term_end is None:
        default_start, default_end = default_term_dates(schedule.semester)
        term_start = default_start if term_start is None else term_start
        term_end = default_end if term_end is None else term_end
    return term_start, term_end


def _courses_by_code(session: Session, codes: set[str]) -> dict[str, Course]:
    codes = {c for c in codes if c}
    if not codes:
        return {}
    return {
        c.code: c
        for c in session.exec(select(Course).where(Course.code.in_(codes))).all()  # type: ignore
    }


def _rooms_by_id(session: Session, ids: set[str]) -> dict[str, Room]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        r.id: r
        for r in session.exec(select(Room).where(Room.id.in_(ids))).all()  # type: ignore
    }


def _slots_by_id(session: Session, ids: set[int]) -> dict[int, TimeSlot]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {
        t.id: t
        for t in session.exec(select(TimeSlot).where(TimeSlot.id.in_(ids))).all()  # type: ignore
    }


def _faculty_by_id(session: Session, ids: set[str]) -> dict[str, Faculty]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        f.id: f
        for f in session.exec(select(Faculty).where(Faculty.id.in_(ids))).all()  # type: ignore
    }


def _meetings_for_sections(
    session: Session, section_ids: list[int]
) -> list[SectionMeeting]:
    section_ids = [i for i in section_ids if i is not None]
    if not section_ids:
        return []
    return list(
        session.exec(
            select(SectionMeeting)
            .where(SectionMeeting.section_id.in_(section_ids))  # type: ignore
            .order_by(SectionMeeting.section_id, SectionMeeting.meeting_sequence)  # type: ignore
        ).all()
    )


def _enrollment_counts(session: Session, section_ids: list[int]) -> dict[int, int]:
    section_ids = [i for i in section_ids if i is not None]
    if not section_ids:
        return {}
    return dict(
        session.exec(
            select(SectionEnrollment.section_id, func.count())
            .where(SectionEnrollment.section_id.in_(section_ids))  # type: ignore
            .group_by(SectionEnrollment.section_id)
        ).all()
    )


def ics_for_instructor(
    session: Session,
    schedule_id: int,
    faculty_id: str,
    term_start: date | None = None,
    term_end: date | None = None,
) -> str:
    """All sections in `schedule_id` taught by `faculty_id`, as an .ics file.

    DESCRIPTION includes the enrolled headcount (one cheap grouped query,
    not one query per section).

    Raises:
        ValueError: if the schedule or faculty member does not exist.
    """
    schedule = _get_schedule(session, schedule_id)
    faculty = session.get(Faculty, faculty_id)
    if faculty is None:
        raise ValueError(f"Faculty {faculty_id!r} not found")
    term_start, term_end = _resolve_term(schedule, term_start, term_end)

    sections = list(
        session.exec(
            select(Section).where(
                Section.schedule_id == schedule_id, Section.faculty_id == faculty_id
            )
        ).all()
    )
    sections_by_id = {s.id: s for s in sections}
    section_ids = list(sections_by_id)

    courses = _courses_by_code(session, {s.course_code for s in sections})
    meetings = _meetings_for_sections(session, section_ids)
    rooms = _rooms_by_id(session, {m.room_id for m in meetings})
    slots = _slots_by_id(session, {m.time_slot_id for m in meetings})
    enrolled = _enrollment_counts(session, section_ids)

    entries: list[_Entry] = []
    for m in meetings:
        section = sections_by_id.get(m.section_id)
        slot = slots.get(m.time_slot_id)
        if section is None or slot is None:
            continue
        course = courses.get(section.course_code)
        room = rooms.get(m.room_id)
        count = enrolled.get(section.id, 0)
        description = f"CRN {section.crn} · Enrolled: {count}"
        entries.append((section, course, m, slot, room, description))

    entries.sort(key=lambda e: (e[0].crn, e[2].meeting_sequence))
    cal_name = f"{faculty.name} — {schedule.semester}"
    return _render_calendar(schedule_id, cal_name, entries, term_start, term_end)


def ics_for_room(
    session: Session,
    schedule_id: int,
    room_id: str,
    term_start: date | None = None,
    term_end: date | None = None,
) -> str:
    """All meetings held in `room_id` within `schedule_id`, as an .ics file.

    Raises:
        ValueError: if the schedule or room does not exist.
    """
    schedule = _get_schedule(session, schedule_id)
    room = session.get(Room, room_id)
    if room is None:
        raise ValueError(f"Room {room_id!r} not found")
    term_start, term_end = _resolve_term(schedule, term_start, term_end)

    sections = list(
        session.exec(
            select(Section).where(Section.schedule_id == schedule_id)
        ).all()
    )
    sections_by_id = {s.id: s for s in sections}
    section_ids = list(sections_by_id)

    courses = _courses_by_code(session, {s.course_code for s in sections})
    meetings = [
        m for m in _meetings_for_sections(session, section_ids) if m.room_id == room_id
    ]
    slots = _slots_by_id(session, {m.time_slot_id for m in meetings})

    entries: list[_Entry] = []
    for m in meetings:
        section = sections_by_id.get(m.section_id)
        slot = slots.get(m.time_slot_id)
        if section is None or slot is None:
            continue
        course = courses.get(section.course_code)
        description = f"CRN {section.crn}"
        entries.append((section, course, m, slot, room, description))

    entries.sort(key=lambda e: (e[0].crn, e[2].meeting_sequence))
    cal_name = f"{room_id} — {schedule.semester}"
    return _render_calendar(schedule_id, cal_name, entries, term_start, term_end)


def ics_for_student(
    session: Session,
    schedule_id: int,
    student_id: str,
    term_start: date | None = None,
    term_end: date | None = None,
) -> str:
    """Sections `student_id` is enrolled in within `schedule_id`, as .ics.

    DESCRIPTION includes the instructor's name when the section has one.

    Raises:
        ValueError: if the schedule or student does not exist.
    """
    schedule = _get_schedule(session, schedule_id)
    student = session.get(Student, student_id)
    if student is None:
        raise ValueError(f"Student {student_id!r} not found")
    term_start, term_end = _resolve_term(schedule, term_start, term_end)

    sections = list(
        session.exec(
            select(Section).where(Section.schedule_id == schedule_id)
        ).all()
    )
    sections_by_id = {s.id: s for s in sections}
    section_ids = set(sections_by_id)

    enrolled_section_ids = {
        e.section_id
        for e in session.exec(
            select(SectionEnrollment).where(SectionEnrollment.student_id == student_id)
        ).all()
        if e.section_id in section_ids
    }
    enrolled_sections = [sections_by_id[i] for i in enrolled_section_ids]

    courses = _courses_by_code(session, {s.course_code for s in enrolled_sections})
    faculty = _faculty_by_id(
        session, {s.faculty_id for s in enrolled_sections if s.faculty_id}
    )
    meetings = _meetings_for_sections(session, list(enrolled_section_ids))
    rooms = _rooms_by_id(session, {m.room_id for m in meetings})
    slots = _slots_by_id(session, {m.time_slot_id for m in meetings})

    entries: list[_Entry] = []
    for m in meetings:
        section = sections_by_id.get(m.section_id)
        slot = slots.get(m.time_slot_id)
        if section is None or slot is None:
            continue
        course = courses.get(section.course_code)
        room = rooms.get(m.room_id)
        instructor = faculty.get(section.faculty_id) if section.faculty_id else None
        description = f"CRN {section.crn}"
        if instructor is not None:
            description += f" · Instructor: {instructor.name}"
        entries.append((section, course, m, slot, room, description))

    entries.sort(key=lambda e: (e[0].crn, e[2].meeting_sequence))
    cal_name = f"{student.name} — {schedule.semester}"
    return _render_calendar(schedule_id, cal_name, entries, term_start, term_end)
