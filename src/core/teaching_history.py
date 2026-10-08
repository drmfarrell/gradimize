"""Historical-teaching derivation importer.

Turns historical Banner schedule extracts (schedule_*.csv, see
exports/Gradimize_Data_Request.md §6/§7) into SEED data for two Faculty
fields:

- which courses an instructor has actually taught -> Faculty.approved_courses
- their typical teaching-time patterns -> Faculty.preferences["preferred_slots"],
  the only preference key the solver's faculty preference bonus reads (see
  ``faculty.preferences.get("preferred_slots", [])`` in src/core/penalties.py).

A human-readable ``derived_history`` summary (course/term counts, day
pattern frequencies, morning/afternoon share) is stored alongside it under
Faculty.preferences["derived_history"] for department review. The solver
never reads that key (confirmed by grepping every ``.preferences`` access
in src/ and scripts/); it is safe cargo.

IMPORTANT engine semantic: Faculty.approved_courses == [] means "can teach
anything" (src/database/models.py). Filling it in NARROWS what that
instructor can be assigned. This importer only ever fills an EMPTY list
(or, with --overwrite, replaces an existing one) -- it never appends to a
list a department already curated, and it never auto-creates Faculty rows
for instructor IDs it doesn't recognize.
"""

import csv
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from src.database.models import Faculty, TimeSlot

logger = logging.getLogger(__name__)

NARROWING_WARNING = (
    "NOTE: an empty Faculty.approved_courses list means 'can teach "
    "anything' to the solver. Filling it in NARROWS what that instructor "
    "can be assigned -- departments must review and add any missing "
    "course codes before this data is trusted for a live solve."
)


@dataclass
class InstructorHistory:
    """Raw teaching signal for one INSTRUCTOR_ID, aggregated across terms."""

    courses: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    meeting_pattern_counts: Counter = field(default_factory=Counter)  # (days, start) -> n
    day_pattern_counts: Counter = field(default_factory=Counter)  # days -> n
    morning_meetings: int = 0
    afternoon_meetings: int = 0


def find_schedule_csvs(directory: "Path | str") -> list[Path]:
    """Return schedule_*.csv files in ``directory``, sorted by name."""
    return sorted(Path(directory).glob("schedule_*.csv"))


def collect_teaching_history(
    paths: list["Path | str"],
) -> tuple[dict[str, InstructorHistory], dict]:
    """Parse one or more schedule CSVs into per-instructor teaching history.

    Case-insensitive headers, utf-8-sig tolerant, optional columns
    tolerated. Rows with a blank INSTRUCTOR_ID are skipped. One row per
    meeting time (a CRN can span multiple rows). A row with blank
    MEETING_DAYS/START_TIME (an online section) still counts toward
    approved_courses but not toward pattern/time-of-day stats.
    """
    history: dict[str, InstructorHistory] = defaultdict(InstructorHistory)
    stats = {"files": 0, "rows": 0, "rows_skipped_no_instructor": 0}

    for p in paths:
        path = Path(p)
        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(StringIO(text))
        norm = {h: h.strip().upper() for h in (reader.fieldnames or [])}
        stats["files"] += 1
        for raw in reader:
            row = {norm[k]: (v or "").strip() for k, v in raw.items() if k in norm}
            if not any(row.values()):
                continue
            stats["rows"] += 1
            instructor_id = row.get("INSTRUCTOR_ID", "")
            if not instructor_id:
                stats["rows_skipped_no_instructor"] += 1
                continue

            term = row.get("TERM", "")
            code = (row.get("SUBJECT", "") + row.get("COURSE_NUMBER", "")).replace(
                " ", ""
            ).upper()
            h = history[instructor_id]
            if code:
                h.courses[code].add(term)

            days = row.get("MEETING_DAYS", "").upper()
            start_time = row.get("START_TIME", "")
            if not days or not start_time:
                continue  # online / no fixed meeting time: skip pattern stats

            h.meeting_pattern_counts[(days, start_time)] += 1
            h.day_pattern_counts[days] += 1
            try:
                hour = int(start_time.split(":")[0])
            except (ValueError, IndexError):
                continue
            if hour < 12:
                h.morning_meetings += 1
            else:
                h.afternoon_meetings += 1

    return history, stats


def _dominant_meeting_patterns(h: InstructorHistory) -> list[tuple[str, str]]:
    """The (days, start_time) pair(s) tied for most-frequent for this instructor."""
    if not h.meeting_pattern_counts:
        return []
    max_n = max(h.meeting_pattern_counts.values())
    return [k for k, n in h.meeting_pattern_counts.items() if n == max_n]


def _summary(h: InstructorHistory) -> dict:
    total = h.morning_meetings + h.afternoon_meetings
    return {
        "courses": {code: len(terms) for code, terms in sorted(h.courses.items())},
        "day_patterns": dict(h.day_pattern_counts.most_common()),
        "meetings_analyzed": total,
        "morning_share": round(h.morning_meetings / total, 2) if total else None,
        "afternoon_share": round(h.afternoon_meetings / total, 2) if total else None,
    }


def apply_teaching_history(
    session: Session,
    history: dict[str, InstructorHistory],
    overwrite: bool = False,
    emit: Callable[[str], None] = lambda line: None,
) -> dict:
    """Write derived history onto matching Faculty rows.

    Fill-only by default: Faculty.approved_courses is only set when
    currently empty, and preference keys (``preferred_slots``,
    ``derived_history``) are only added when absent. ``--overwrite``
    replaces both regardless. Faculty rows are matched by INSTRUCTOR_ID ==
    Faculty.id; unmatched IDs are reported, never auto-created.
    """
    slots_by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    for slot in session.exec(select(TimeSlot)).all():
        slots_by_key[(slot.pattern.upper(), slot.start_time)].append(slot.id)

    faculty_by_id = {f.id: f for f in session.exec(select(Faculty)).all()}

    stats = {
        "faculty_updated": 0,
        "faculty_skipped_nonempty": 0,
        "unknown_instructor_ids": [],
        "reports": [],
    }

    emit(NARROWING_WARNING)

    for instructor_id in sorted(history):
        h = history[instructor_id]
        faculty = faculty_by_id.get(instructor_id)
        if faculty is None:
            stats["unknown_instructor_ids"].append(instructor_id)
            continue

        courses_before = list(faculty.approved_courses)
        derived_courses = sorted(h.courses.keys())

        courses_changed = False
        if not faculty.approved_courses or overwrite:
            faculty.approved_courses = derived_courses
            courses_changed = True
        else:
            stats["faculty_skipped_nonempty"] += 1

        preferred_slot_ids = sorted(
            {
                sid
                for pattern, start_time in _dominant_meeting_patterns(h)
                for sid in slots_by_key.get((pattern, start_time), [])
            }
        )

        prefs_before = dict(faculty.preferences)
        prefs = dict(faculty.preferences)
        prefs_changed = False
        if preferred_slot_ids and (overwrite or "preferred_slots" not in prefs):
            prefs["preferred_slots"] = preferred_slot_ids
            prefs_changed = True
        if overwrite or "derived_history" not in prefs:
            prefs["derived_history"] = _summary(h)
            prefs_changed = True
        if prefs_changed:
            faculty.preferences = prefs

        if courses_changed or prefs_changed:
            session.add(faculty)
            stats["faculty_updated"] += 1

        line = (
            f"{instructor_id} ({faculty.name}): approved_courses "
            f"{courses_before} -> {faculty.approved_courses}"
            f"{' [unchanged, already curated]' if not courses_changed else ''}; "
            f"preferred_slots {prefs_before.get('preferred_slots', [])} -> "
            f"{faculty.preferences.get('preferred_slots', [])}"
        )
        stats["reports"].append(line)
        emit(line)

    return stats


def derive_teaching_history(
    session: Session,
    source: "Path | str | list",
    overwrite: bool = False,
    dry_run: bool = False,
    emit: Callable[[str], None] = lambda line: None,
) -> dict:
    """End-to-end: parse schedule CSVs, apply to Faculty, commit (or not).

    ``source`` is either a directory containing schedule_*.csv files or an
    explicit list of CSV paths. Single transaction: everything is
    committed at the end, unless ``dry_run`` is set, in which case the
    session is rolled back and nothing is persisted.
    """
    if isinstance(source, (list, tuple)):
        paths = list(source)
    else:
        paths = find_schedule_csvs(source)

    history, parse_stats = collect_teaching_history(paths)
    apply_stats = apply_teaching_history(
        session, history, overwrite=overwrite, emit=emit
    )

    stats = {**parse_stats, **apply_stats}
    stats["faculty_seen"] = sorted(history.keys())
    stats["dry_run"] = dry_run

    if dry_run:
        session.rollback()
        emit("DRY RUN: no changes committed.")
    else:
        session.commit()

    logger.info("Teaching history derivation: %s", stats)
    return stats
