"""Shared time/slot arithmetic for Gradimize.

The canonical home for "HH:MM" parsing, time-range overlap, and slot
overlap. Earlier versions carried three copies of time_to_minutes and four
independent overlap implementations that had started to diverge.
"""

from src.database.models import TimeSlot

_TERM_CODE_TO_NAME = {
    "FA": "FALL", "F": "FALL", "FALL": "FALL",
    "SP": "SPRING", "S": "SPRING", "SPRING": "SPRING",
    "SU": "SUMMER", "SUMMER": "SUMMER",
    "WI": "WINTER", "WINTER": "WINTER",
}


def time_to_minutes(time_str: str) -> int:
    """Convert "HH:MM" to minutes since midnight."""
    hours, minutes = map(int, time_str.split(":"))
    return hours * 60 + minutes


def times_overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    """Check if two "HH:MM" time ranges overlap."""
    s1, e1 = time_to_minutes(start1), time_to_minutes(end1)
    s2, e2 = time_to_minutes(start2), time_to_minutes(end2)
    return s1 < e2 and s2 < e1


def slots_overlap(a: TimeSlot, b: TimeSlot) -> bool:
    """Check if two time slots conflict: share a day AND their times overlap."""
    if not set(a.days) & set(b.days):
        return False
    return times_overlap(a.start_time, a.end_time, b.start_time, b.end_time)


def parse_semester(sem: str) -> tuple[int, str]:
    """Parse a semester code like '2025FA' into (year, term_name).

    Raises ValueError on input we can't interpret.
    """
    s = sem.strip().upper()
    if len(s) < 5 or not s[:4].isdigit():
        raise ValueError(f"Invalid semester code: {sem!r}")
    year = int(s[:4])
    code = s[4:].strip()
    if code not in _TERM_CODE_TO_NAME:
        raise ValueError(f"Unknown term suffix in semester: {sem!r}")
    return year, _TERM_CODE_TO_NAME[code]
