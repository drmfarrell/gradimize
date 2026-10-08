"""Registrations → demand importer (back-test "Design A").

Turns a Banner registrations extract (one row per active registration; see
exports/Gradimize_Data_Request.md §3) into StudentDemand rows: observed,
revealed demand instead of the internal calculator's predicted demand.
Urgency still uses the shared formula so graduation-critical logic and
conflict weighting behave identically across demand sources.
"""

import csv
import logging
from io import StringIO
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from src.core.graph import PrerequisiteGraph
from src.core.runlog import RunLog
from src.core.urgency import score_base_urgency
from src.database.models import Course, DegreeRequirement, Student, StudentDemand

logger = logging.getLogger(__name__)

# Per-occurrence run-log events are capped; the summary stats carry the full
# counts regardless (a 5000-row unknown-student cascade needs 1 number, not
# 5000 events).
MAX_ROW_EVENTS = 50


def import_registrations_as_demand(
    session: Session,
    source: "Path | str",
    clear_existing: bool = True,
    runlog: RunLog | None = None,
) -> dict:
    """Import registrations.csv, creating one demand per (student, course).

    Expected columns (case-insensitive): student_id, term, crn, and either
    course_code or subject + course_number. An optional `status` column
    (Banner SFRSTCR_RSTS_CODE) keeps the demand honest when the extract
    contains ALL registration rows rather than pre-filtered ones:
    R* (registered) counts as demand, WL* (waitlisted) counts as demand —
    it is exactly the demand the existing schedule failed to seat — and
    anything else (drops, withdrawals, audits) is excluded and tallied per
    status code in the returned stats. A blank/absent status behaves like
    today: every row counts. Unknown students/courses are skipped and
    reported. Runs in one transaction: existing non-projected demands are
    cleared uncommitted and the single commit at the end lands everything
    atomically.
    """
    runlog = runlog or RunLog.disabled()
    text = Path(source).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    norm = {h: h.strip().lower().replace(" ", "_") for h in (reader.fieldnames or [])}

    students = {s.id: s for s in session.exec(select(Student)).all()}
    courses = {c.code for c in session.exec(select(Course)).all()}
    required_by_major: dict[tuple[str, int], set[str]] = {}
    for r in session.exec(select(DegreeRequirement)).all():
        required_by_major.setdefault((r.major, r.catalog_year), set()).add(r.course_code)

    graph = PrerequisiteGraph(session)

    if clear_existing:
        session.execute(
            sa_delete(StudentDemand).where(
                StudentDemand.is_projected == False  # noqa: E712
            )
        )

    stats = {"rows": 0, "demands_created": 0, "students_seen": 0,
             "skipped_unknown_student": 0, "skipped_unknown_course": 0,
             "waitlisted_as_demand": 0}
    skipped_by_status: dict[str, int] = {}
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    terms: set[str] = set()

    for raw in reader:
        row = {norm[k]: (v or "").strip() for k, v in raw.items() if k in norm}
        if not any(row.values()):
            continue
        stats["rows"] += 1
        sid = row.get("student_id", "")
        code = row.get("course_code") or (
            row.get("subject", "") + row.get("course_number", "")
        )
        code = code.replace(" ", "").upper()
        if row.get("term"):
            terms.add(row["term"])

        # Registration status: R* = registered, WL* = waitlisted (both are
        # real demand); anything else (DD/DW/W... drops, AU audits) is not.
        status = row.get("status", "").upper()
        waitlisted = status.startswith("WL")
        if status and not status.startswith("R") and not waitlisted:
            skipped_by_status[status] = skipped_by_status.get(status, 0) + 1
            continue

        student = students.get(sid)
        if student is None:
            stats["skipped_unknown_student"] += 1
            if len(errors) < 20:
                errors.append(f"Unknown student {sid!r} (row {stats['rows']})")
            # Run log: row number only — the id itself is student data.
            if stats["skipped_unknown_student"] <= MAX_ROW_EVENTS:
                runlog.event("unknown_student", file="registrations.csv",
                             row=stats["rows"])
            continue
        if code not in courses:
            stats["skipped_unknown_course"] += 1
            if len(errors) < 20:
                errors.append(f"Unknown course {code!r} (row {stats['rows']})")
            # Course codes are catalog data — safe to name in the run log.
            if stats["skipped_unknown_course"] <= MAX_ROW_EVENTS:
                runlog.event("unknown_course", file="registrations.csv",
                             row=stats["rows"], code=code)
            continue
        if (sid, code) in seen:
            continue
        seen.add((sid, code))

        is_required = code in required_by_major.get(
            (student.major, student.catalog_year), set()
        )
        urgency, reasons, grad_critical = score_base_urgency(
            student.class_standing,
            is_required,
            len(graph.get_all_dependents(code)),
        )
        label = ("Waitlisted (observed unmet demand)" if waitlisted
                 else "Registered (observed demand)")
        session.add(StudentDemand(
            student_id=sid, course_code=code, urgency_score=urgency,
            is_required=is_required, is_graduation_critical=grad_critical,
            reason="; ".join([label] + reasons),
        ))
        stats["demands_created"] += 1
        if waitlisted:
            stats["waitlisted_as_demand"] += 1

    session.commit()
    stats["students_seen"] = len({s for s, _ in seen})
    stats["terms"] = sorted(terms)
    stats["skipped_by_status"] = skipped_by_status
    stats["errors"] = errors
    # Everything except the errors list (which quotes student ids) is
    # aggregate: counts, term codes, status-code tallies. Keep the errors out
    # of BOTH log channels; callers still get them in the returned stats.
    safe_stats = {k: v for k, v in stats.items() if k != "errors"}
    logger.info("Registrations import: %s (%d row errors)", safe_stats, len(errors))
    runlog.event("registrations_import",
                 **safe_stats,
                 n_errors=len(errors))
    return stats
