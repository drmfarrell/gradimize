"""Data-health checks ("doctor") — vet imported data before anyone solves.

Runs a battery of checks over the current database and returns structured
results graded ok / warn / fail:

- referential integrity (history/demands/prereqs pointing at real rows)
- solvability (courses with no compatible room/slot, staffing bottlenecks
  via the engine's pre-flight, seat ceilings)
- data quality (duplicates, unknown grades, standing-vs-credits mismatches,
  contact-hour compliance)
- environment (database location, config, time grid)

This is the "validate a small sample before the full extract" step from the
data-request workflow: run it right after importing a delivery and fix every
FAIL before generating schedules. Surfaces: `python scripts/doctor.py`,
the TUI dashboard [h], and `GET /api/data/health`.
"""

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from src.database.models import (
    Course,
    DegreeRequirement,
    Faculty,
    Prerequisite,
    Room,
    SemesterGuideEntry,
    StructureType,
    Student,
    StudentCourseHistory,
    StudentDemand,
    TimeSlot,
)
from src.database.queries import CREDIT_BEARING_NON_LETTER_GRADES, LETTER_GRADE_ORDER

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 5

# prereq-chain-vs-cadence: a normal undergraduate plan is 8 regular
# (FALL/SPRING) terms; summer is deliberately excluded as a planning term
# even if a course lists it in offer_terms — a chain rescued only by summer
# availability is still flagged (the conservative reading). The horizon runs
# 12 terms (~6 calendar years) so there's headroom past the 8-term budget to
# tell "eventually offered, just late" apart from "never offered at all".
PREREQ_HORIZON_TERMS = 12
PREREQ_MAX_REGULAR_TERMS = 8

# Tolerant: real extracts carry withdrawals, incompletes, pass/fail, and
# transfer markers alongside letter grades. Built from the same constants
# get_completed_courses uses so "recognized" and "counts as passing" can
# never drift apart again.
KNOWN_GRADES = (
    set(LETTER_GRADE_ORDER)
    | CREDIT_BEARING_NON_LETTER_GRADES
    | {"F", "W", "WF", "WP", "I", "NP", "U", "NC", "AU"}
)

# Rough credit bands per class standing, generous at the edges. Warn-level
# only: registrars derive standing differently, but a SR with 10 credits
# usually means the extract mixed columns up.
STANDING_BANDS = {
    "FR": (0, 44),
    "SO": (15, 74),
    "JR": (45, 104),
    "SR": (75, 10_000),
}


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    summary: str
    count: int = 0
    details: list[str] = field(default_factory=list)
    # True when details contain FERPA-protected student data (ids, grades,
    # per-student attributes). Summaries are always count-only and safe.
    # Consumers that persist or export results (the run log, the CLI's
    # default output) must redact details of protected checks; the web
    # dashboard — the operator's own screen — shows them.
    protected: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "count": self.count,
            "details": self.details,
            "protected": self.protected,
        }


def _result(name, status, summary, examples=None, protected=False):
    examples = list(examples or [])
    return CheckResult(
        name=name,
        status=status,
        summary=summary,
        count=len(examples),
        details=[str(e) for e in examples[:MAX_EXAMPLES]],
        protected=protected,
    )


def _graded(name, ok_summary, bad, bad_status, bad_summary, protected=False):
    """ok result when `bad` is empty, else a warn/fail with examples."""
    if not bad:
        return CheckResult(name=name, status="ok", summary=ok_summary)
    r = _result(name, bad_status, bad_summary.format(n=len(bad)), bad,
                protected=protected)
    r.count = len(bad)
    return r


def overall_status(results: list[CheckResult]) -> str:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"


def run_health_checks(session: Session, deep: bool = True,
                      runlog=None) -> list[CheckResult]:
    """Run all checks; `deep=False` skips the solver-backed staffing check.

    Every run also writes a FERPA-safe record to the run log (details of
    `protected` checks redacted to counts — see src/core/runlog.py). Pass a
    RunLog to join an existing run; by default each call opens its own.
    """
    from src.core.runlog import RunLog

    own_runlog = runlog is None
    runlog = runlog or RunLog("doctor")
    try:
        results = _run_health_checks(session, deep)
        for r in results:
            extra = ({"details": r.details} if not r.protected
                     else {"details_redacted": True})
            runlog.event("check", name=r.name, status=r.status,
                         summary=r.summary, count=r.count, **extra)
        runlog.event("overall", status=overall_status(results),
                     n_checks=len(results))
        return results
    finally:
        if own_runlog:
            runlog.close()


def _run_health_checks(session: Session, deep: bool = True) -> list[CheckResult]:
    results: list[CheckResult] = []

    students = {s.id: s for s in session.exec(select(Student)).all()}
    courses = {c.code: c for c in session.exec(select(Course)).all()}
    rooms = [r for r in session.exec(select(Room)).all()]
    slots = list(session.exec(select(TimeSlot)).all())
    faculty = list(session.exec(select(Faculty)).all())
    history = list(session.exec(select(StudentCourseHistory)).all())
    demands = list(session.exec(select(StudentDemand)).all())
    prereqs = list(session.exec(select(Prerequisite)).all())
    degree_reqs = list(session.exec(select(DegreeRequirement)).all())
    guide_entries = list(session.exec(select(SemesterGuideEntry)).all())

    active_room_types = {
        (r.room_type.value if hasattr(r.room_type, "value") else r.room_type)
        for r in rooms
        if r.is_active
    }
    slot_types = {s.slot_type for s in slots}
    demanded_courses = {d.course_code for d in demands}

    # ── referential integrity ────────────────────────────────────────
    results.append(_graded(
        "history-orphan-students",
        f"All {len(history)} course-history rows reference known students",
        sorted({h.student_id for h in history if h.student_id not in students}),
        "fail",
        "{n} student id(s) in course history don't exist in students — "
        "pseudonym key mismatch between refreshes, or a partial delivery",
        protected=True,
    ))
    results.append(_graded(
        "history-unknown-courses",
        "All course-history rows reference cataloged courses",
        sorted({h.course_code for h in history if h.course_code not in courses}),
        "warn",
        "{n} course code(s) in history aren't in the catalog (transfer/"
        "retired courses are normal; prerequisite checks can't see them)",
    ))
    results.append(_graded(
        "demand-orphan-students",
        f"All {len(demands)} demand rows reference known students",
        sorted({d.student_id for d in demands if d.student_id not in students}),
        "fail",
        "{n} student id(s) in demands don't exist — re-run the demand import "
        "after fixing the student extract",
        protected=True,
    ))
    results.append(_graded(
        "demand-unknown-courses",
        "All demand rows reference cataloged courses",
        sorted({d.course_code for d in demands if d.course_code not in courses}),
        "fail",
        "{n} course code(s) in demands aren't in the catalog — these demands "
        "can never be scheduled",
    ))
    results.append(_graded(
        "prereq-unknown-courses",
        f"All {len(prereqs)} prerequisite edges reference cataloged courses",
        sorted({c for p in prereqs for c in (p.course_code, p.prereq_code)
                if c not in courses}),
        "warn",
        "{n} course code(s) in prerequisites aren't in the catalog",
    ))
    results.append(_graded(
        "degreereq-unknown-courses",
        f"All {len(degree_reqs)} degree requirements reference cataloged courses",
        sorted({r.course_code for r in degree_reqs if r.course_code not in courses}),
        "warn",
        "{n} course code(s) in degree requirements aren't in the catalog — "
        "those requirements can never mark a course required",
    ))
    results.append(_graded(
        "guide-unknown-courses",
        "All semester-guide entries reference cataloged courses",
        sorted({g.course_code for g in guide_entries
                if g.course_code and g.course_code not in courses}),
        "warn",
        "{n} course code(s) in semester guides aren't in the catalog",
    ))

    bad_links = []
    for c in courses.values():
        if c.structure_type == StructureType.CHILD:
            parent = courses.get(c.linked_course_code or "")
            if parent is None:
                bad_links.append(f"{c.code} → missing {c.linked_course_code!r}")
            elif parent.structure_type != StructureType.PARENT:
                bad_links.append(
                    f"{c.code} → {parent.code} is {parent.structure_type.value}, "
                    f"not PARENT"
                )
    results.append(_graded(
        "linked-course-integrity",
        "Every CHILD course links to an existing PARENT",
        bad_links,
        "fail",
        "{n} CHILD course(s) with broken parent links — linked no-overlap "
        "constraints will silently not apply",
    ))

    # ── solvability ──────────────────────────────────────────────────
    results.append(_check_prereq_chain_cadence(courses, prereqs, degree_reqs))

    no_room, no_room_undemanded = [], []
    no_slot, no_slot_undemanded = [], []
    for c in courses.values():
        required = set(c.required_room_types or ["LECTURE"])
        if not (required & active_room_types):
            (no_room if c.code in demanded_courses else no_room_undemanded).append(
                f"{c.code} needs {sorted(required)}"
            )
        valid = set(c.valid_slot_types or [])
        if valid and not (valid & slot_types):
            (no_slot if c.code in demanded_courses else no_slot_undemanded).append(
                f"{c.code} allows {sorted(valid)}"
            )
    results.append(_graded(
        "course-no-compatible-room",
        "Every demanded course has at least one active room of a usable type",
        no_room, "fail",
        "{n} demanded course(s) require room types with NO active room — "
        "unschedulable until a room is added/activated or the course fixed",
    ))
    if no_room_undemanded:
        results.append(_result(
            "course-no-compatible-room-undemanded", "warn",
            f"{len(no_room_undemanded)} un-demanded course(s) require room "
            f"types with no active room", no_room_undemanded))
    results.append(_graded(
        "course-no-matching-slots",
        "Every demanded course's slot types exist in the time grid",
        no_slot, "fail",
        "{n} demanded course(s) allow only slot types absent from the time "
        "grid — unschedulable",
    ))
    if no_slot_undemanded:
        results.append(_result(
            "course-no-matching-slots-undemanded", "warn",
            f"{len(no_slot_undemanded)} un-demanded course(s) allow only "
            f"missing slot types", no_slot_undemanded))

    xl_groups: dict[str, list[Course]] = {}
    for c in courses.values():
        if c.crosslist_group:
            xl_groups.setdefault(c.crosslist_group, []).append(c)
    xl_issues: list[str] = []
    for gid, members in xl_groups.items():
        if len(members) == 1:
            xl_issues.append(
                f"group {gid!r}: only {members[0].code} — nothing to cross-list with"
            )
            continue
        codes = "/".join(sorted(m.code for m in members))
        if len({tuple(sorted(m.valid_slot_types or [])) for m in members}) > 1:
            xl_issues.append(
                f"{codes}: members disagree on valid_slot_types — shared "
                f"placement may be impossible"
            )
        if len({tuple(sorted(m.required_room_types or [])) for m in members}) > 1:
            xl_issues.append(
                f"{codes}: members disagree on required_room_types — shared "
                f"room may be impossible"
            )
        if len({(m.cadence, tuple(sorted(m.offer_terms or []))) for m in members}) > 1:
            xl_issues.append(
                f"{codes}: members disagree on cadence/offer_terms — "
                f"they must be offered together"
            )
    results.append(_graded(
        "crosslist-consistency",
        "Cross-listed groups are consistent"
        if xl_groups else "No cross-listed groups defined",
        xl_issues, "warn",
        "{n} cross-list issue(s) — mismatched members can make the shared "
        "class impossible to place",
    ))

    generalists = [f for f in faculty if not f.approved_courses]
    unstaffable: list[str] = []
    if faculty and not generalists:
        demand_counts = Counter(d.course_code for d in demands)
        for code in sorted(demanded_courses):
            if code in courses and not any(
                code in (f.approved_courses or []) for f in faculty
            ):
                unstaffable.append(f"{code} ({demand_counts[code]} demand(s))")
    results.append(_graded(
        "unstaffable-courses",
        "Every demanded course has at least one qualified instructor",
        unstaffable, "fail",
        "{n} demanded course(s) that NO instructor can teach — nobody lists "
        "them in approved_courses and there are no unrestricted instructors "
        "— sections would run with no instructor assigned. Qualify an "
        "instructor (approved_courses; max_overload_credits allows paid "
        "overloads) or hire",
    ))

    if demands and not faculty:
        results.append(_result(
            "no-faculty", "fail",
            "Demands exist but there are zero faculty — every section needs "
            "an instructor", []))
    elif slots and not rooms:
        results.append(_result(
            "no-rooms", "fail", "No rooms loaded — nothing can be scheduled", []))
    else:
        results.append(CheckResult(
            name="basic-resources", status="ok",
            summary=f"{len(faculty)} faculty, {len(rooms)} rooms, "
                    f"{len(slots)} time slots loaded"))

    if deep and demands and courses and slots and rooms:
        results.append(_staffing_check(session))

    # ── data quality ─────────────────────────────────────────────────
    dup_hist = [
        f"{sid} {code} {sem} ×{n}"
        for (sid, code, sem), n in Counter(
            (h.student_id, h.course_code, h.semester) for h in history
        ).items()
        if n > 1
    ]
    results.append(_graded(
        "duplicate-history-rows",
        "No duplicate (student, course, semester) history rows",
        dup_hist, "warn",
        "{n} duplicated course-history row group(s) — usually a double "
        "delivery; harmless to prereq checks but inflates credit sums",
        protected=True,
    ))

    dup_dem = [
        f"{sid} {code} ×{n}"
        for (sid, code), n in Counter(
            (d.student_id, d.course_code) for d in demands
        ).items()
        if n > 1
    ]
    results.append(_graded(
        "duplicate-demand-rows",
        "No duplicate (student, course) demand rows",
        dup_dem, "fail",
        "{n} duplicated demand pair(s) — double-counts urgency in the "
        "objective; re-import demands with a clearing import",
        protected=True,
    ))

    weird_grades = sorted({
        h.grade.strip().upper() for h in history
        if h.grade and h.grade.strip().upper() not in KNOWN_GRADES
    })
    results.append(_graded(
        "unknown-grades",
        "All history grades are recognized",
        weird_grades, "warn",
        "{n} unrecognized grade value(s) — these rows can't satisfy "
        "min-grade prerequisite rules",
        # Grades are FERPA-protected even unlinked — the owner drew the line
        # at "names / IDs, grades".
        protected=True,
    ))

    mismatched = []
    for s in students.values():
        standing = getattr(s.class_standing, "value", s.class_standing)
        band = STANDING_BANDS.get(str(standing))
        if band and not (band[0] <= s.credits_earned <= band[1]):
            mismatched.append(f"{s.id}: {standing} with {s.credits_earned} cr")
    results.append(_graded(
        "standing-credits-mismatch",
        "Class standing and credits earned are consistent",
        mismatched, "warn",
        "{n} student(s) whose standing and earned credits disagree strongly "
        "— check the extract's column mapping",
        protected=True,
    ))

    # Degree-requirement coverage: demand prediction and graduation-critical
    # weighting match on (major, catalog_year) EXACTLY, with no nearest-year
    # fallback — a student under a catalog year with zero DegreeRequirement
    # rows silently gets no required courses and can never be grad-critical.
    # Continuing juniors/seniors are routinely several catalog years back, so
    # requirement sheets must cover every year in the student body. Only
    # meaningful once SOME requirements are transcribed (partial coverage is
    # the silent failure mode; total absence is obvious everywhere).
    if degree_reqs:
        req_pairs = {(r.major, r.catalog_year) for r in degree_reqs}
        uncovered = Counter(
            (s.major, s.catalog_year)
            for s in students.values()
            if s.major and not s.is_projected
            and (s.major, s.catalog_year) not in req_pairs
        )
        results.append(_graded(
            "degree-requirement-coverage",
            "Every (major, catalog year) in the student body has degree requirements",
            [f"{m}/{y}: {n} student(s)" for (m, y), n in uncovered.most_common()],
            "warn",
            "{n} (major, catalog year) group(s) with NO degree requirements — "
            "these students get no required-course or graduation-critical "
            "weighting; transcribe the missing catalog years' requirement sheets",
        ))

    # Contact-hour compliance: accreditors expect weekly contact minutes to
    # track credit hours (~50 min/week per credit). A course passes if AT
    # LEAST ONE of its valid slot types lands within a generous 60%-160% band
    # of credits x 50 — labs separately coded from their lecture run long per
    # credit by design, so this is deliberately loose. Multi-meeting courses
    # (meeting_patterns) define their own per-meeting structure, and CHILD
    # sections are exactly those separately-coded labs — both skipped. Slot
    # types with zero TimeSlot rows are ignored (a course whose valid types
    # are ALL missing is already flagged by course-no-matching-slots).
    slot_type_minutes: dict[str, int] = {}
    for s in slots:
        slot_type_minutes.setdefault(s.slot_type, s.duration_minutes * len(s.days or []))

    contact_checked = 0
    contact_issues = []
    for c in courses.values():
        if c.meeting_patterns or c.structure_type == StructureType.CHILD:
            continue
        options = [slot_type_minutes[t] for t in (c.valid_slot_types or [])
                   if t in slot_type_minutes]
        if not options:
            continue
        contact_checked += 1
        expected = c.credits * 50
        lo, hi = expected * 0.6, expected * 1.6
        if not any(lo <= m <= hi for m in options):
            contact_issues.append(
                f"{c.code}: {c.credits} credits but its slot options deliver "
                f"{min(options)}-{max(options)} min/week (expected ~{expected})"
            )
    results.append(_graded(
        "contact-hours",
        f"{contact_checked} course(s) checked — weekly contact minutes track "
        f"credit hours",
        contact_issues, "warn",
        "{n} course(s) whose valid slot types deliver contact minutes far "
        "from what their credit hours imply (~50 min/week/credit expected)",
    ))

    if demands:
        demanded_students = {d.student_id for d in demands}
        without = [sid for sid in students
                   if sid not in demanded_students
                   and not students[sid].is_projected]
        results.append(_graded(
            "students-without-demands",
            f"Every enrolled student has at least one demand "
            f"({len(demands)} demands, {len(students)} students)",
            without, "warn",
            "{n} student(s) with no demand rows — they're invisible to the "
            "optimizer (fine if genuinely not registering)",
            protected=True,
        ))
    else:
        results.append(_result(
            "no-demands", "warn",
            "No student demands loaded yet — import registrations or a "
            "DegreeWorks audit before generating", []))

    # ── environment ──────────────────────────────────────────────────
    results.append(_environment_check())

    return results


def _staffing_check(session: Session) -> CheckResult:
    """Run the engine's staffing pre-flight against projected sections."""
    try:
        from src.core.conflicts import build_conflict_matrix
        from src.core.engine import SchedulerConfig, SchedulingEngine

        config = SchedulerConfig.load_default()
        engine = SchedulingEngine(session, build_conflict_matrix(session), config)
        sections = engine.create_sections("PREVIEW")
        warnings = engine._staffing_preflight(sections)
        if warnings:
            return _result(
                "staffing-preflight", "fail",
                f"{len(warnings)} staffing bottleneck(s): current demand "
                f"cannot be staffed under teaching-qualification rules", warnings)
        return CheckResult(
            name="staffing-preflight", status="ok",
            summary=f"Projected {len(sections)} sections are staffable under "
                    f"teaching-qualification rules")
    except Exception as e:  # never let the doctor die on a check
        logger.exception("staffing pre-flight check failed")
        # Exception TYPE only: summaries flow into the FERPA-safe run log
        # verbatim, and exception messages can embed data values. Full
        # traceback is on the server console via logger.exception above.
        return _result("staffing-preflight", "warn",
                       f"Could not run staffing pre-flight "
                       f"({type(e).__name__} — see server log)", [])


def _environment_check() -> CheckResult:
    from src.database.connection import DEFAULT_DB_PATH, REPO_DATA_DIR

    db = Path(DEFAULT_DB_PATH)
    if not db.exists():
        return _result("environment", "warn",
                       f"Database not found at {db} — nothing imported yet?", [])
    size_mb = db.stat().st_size / 1e6
    inside_repo = REPO_DATA_DIR in db.parents or db.parent == REPO_DATA_DIR
    if inside_repo:
        return _result(
            "environment", "warn",
            f"Database lives INSIDE the repo ({db}, {size_mb:.1f} MB) — set "
            f"GRADIMIZE_DATA_DIR to keep live data outside the working tree "
            f"(gitignored either way, but the isolation guarantee is stronger)",
            [])
    return CheckResult(
        name="environment", status="ok",
        summary=f"Database at {db} ({size_mb:.1f} MB), outside the repo")


def _check_prereq_chain_cadence(courses, prereqs, degree_reqs) -> CheckResult:
    """Flag prerequisite chains that can't finish in a normal 4-year plan.

    A "normal" plan is 8 regular (FALL/SPRING) terms. BIENNIAL_EVEN /
    BIENNIAL_ODD cadence means a course's availability depends on which
    calendar-year parity a given term falls in, so which parity year a
    student ENTERS in (fall of an even year vs. an odd year) can change how
    many terms a chain needs to finish. Students enter every year, so both
    entry parities are evaluated — either one blowing the 8-term budget is
    enough to flag the chain.

    For every course we compute the earliest 1-based term (within a 12-term
    horizon — see _prereq_earliest_completions) at which it could first be
    completed: the smallest available term at or after every hard
    prerequisite's earliest completion (strictly before) and every
    corequisite's earliest completion (same term allowed).

    Grade: fail if any flagged course is required by some major (a
    graduation blocker); warn if only electives are affected.
    """
    prereqs_by_course: dict[str, list[tuple[str, bool]]] = {}
    for p in prereqs:
        prereqs_by_course.setdefault(p.course_code, []).append(
            (p.prereq_code, p.is_corequisite)
        )
    required_courses = {r.course_code for r in degree_reqs if r.is_required}

    # Only the parity (even/odd) of these anchor years matters to
    # is_off_cadence — the specific years are illustrative.
    even_earliest, even_binding, even_ever = _prereq_earliest_completions(
        courses, prereqs_by_course, 2026)
    odd_earliest, odd_binding, odd_ever = _prereq_earliest_completions(
        courses, prereqs_by_course, 2027)

    flagged: list[tuple[float, str, str]] = []
    for code, course in courses.items():
        worst_even, worst_odd = even_earliest[code], odd_earliest[code]
        worst = max(worst_even, worst_odd)
        if worst <= PREREQ_MAX_REGULAR_TERMS:
            continue

        # Report whichever parity is worse (ties go to "even").
        if worst_even >= worst_odd:
            parity_label = "even"
            term_val, binding_map, ever = worst_even, even_binding, even_ever[code]
        else:
            parity_label = "odd"
            term_val, binding_map, ever = worst_odd, odd_binding, odd_ever[code]

        if not ever:
            msg = (
                f"{code}: never offered on a {PREREQ_HORIZON_TERMS}-term "
                f"horizon (offer_terms={course.offer_terms!r}, "
                f"cadence={course.cadence!r})"
            )
        else:
            chain = _reconstruct_chain(code, binding_map, courses)
            chain_str = " → ".join(chain)
            biennial = [c for c in chain if courses[c].cadence != "ANNUAL"]
            suffix = (
                f"; biennial links: {', '.join(biennial)}" if biennial else ""
            )
            term_phrase = (
                f"beyond the {PREREQ_HORIZON_TERMS}-term horizon"
                if term_val == math.inf else f"term {int(term_val)}"
            )
            msg = (
                f"{code}: earliest completion is {term_phrase} entering "
                f"{parity_label}-year fall (chain {chain_str}{suffix})"
            )
        flagged.append((worst, code, msg))

    # Worst first, so a MAX_EXAMPLES=5 truncation keeps the scariest ones.
    flagged.sort(key=lambda item: item[0], reverse=True)
    bad_all = [msg for _, _, msg in flagged]
    bad_required = [msg for _, code, msg in flagged if code in required_courses]
    status = "fail" if bad_required else "warn"

    return _graded(
        "prereq-chain-vs-cadence",
        "All prerequisite chains completable within 8 regular terms",
        bad_all, status,
        "{n} course(s) unreachable within 8 regular terms given offering cadence",
    )


def _prereq_earliest_completions(courses, prereqs_by_course, start_year):
    """Earliest 1-based completion term for every course, over a
    PREREQ_HORIZON_TERMS-term (FALL/SPRING alternating, no summer) horizon
    starting with `start_year`'s fall term.

    Returns (earliest, binding, ever_available):
    - earliest[code]: smallest term (1..PREREQ_HORIZON_TERMS) at which
      `code` could first be completed given its prerequisite chain, or
      math.inf if that never happens within the horizon.
    - binding[code]: the prerequisite code that set `code`'s earliest lower
      bound — used to reconstruct one representative binding chain. Absent
      for a course whose own availability (not a prerequisite) is what caps
      it, or that has no prerequisites at all.
    - ever_available[code]: whether `code` is offered in ANY of the horizon
      terms at all, ignoring prerequisites entirely. False only when its
      offer_terms excludes both FALL and SPRING (e.g. summer-only) — the
      "never offered" case, distinct from a chain merely running long.
    """
    from src.core.cadence import is_off_cadence

    horizon: list[tuple[int, str]] = []
    year, term = start_year, "FALL"
    for _ in range(PREREQ_HORIZON_TERMS):
        horizon.append((year, term))
        if term == "FALL":
            year, term = year + 1, "SPRING"
        else:
            term = "FALL"

    def available(code: str, t: int) -> bool:
        term_year, term_name = horizon[t - 1]
        return not is_off_cadence(courses[code], term_year, term_name)

    memo: dict[str, float] = {}
    binding: dict[str, str] = {}
    visiting: set[str] = set()

    def earliest(code: str) -> float:
        if code in memo:
            return memo[code]
        if code not in courses or code in visiting:
            # Unknown course (referential-integrity checks own that) or a
            # prerequisite cycle (cycle detection owns that) — impose no
            # constraint rather than crash or recurse forever.
            return 0
        visiting.add(code)
        lower: float = 1
        for prereq_code, is_coreq in prereqs_by_course.get(code, []):
            e = earliest(prereq_code)
            candidate = e if is_coreq else e + 1
            if candidate > lower:
                lower = candidate
                binding[code] = prereq_code
        result: float = math.inf
        if lower <= PREREQ_HORIZON_TERMS:
            for t in range(int(lower), PREREQ_HORIZON_TERMS + 1):
                if available(code, t):
                    result = t
                    break
        visiting.discard(code)
        memo[code] = result
        return result

    earliest_map = {code: earliest(code) for code in courses}
    ever_available = {
        code: any(
            available(code, t) for t in range(1, PREREQ_HORIZON_TERMS + 1)
        )
        for code in courses
    }
    return earliest_map, binding, ever_available


def _reconstruct_chain(code, binding, courses):
    """Walk `binding` pointers from `code` back to its root; root-first."""
    chain = [code]
    seen = {code}
    cur = code
    while True:
        nxt = binding.get(cur)
        if nxt is None or nxt in seen or nxt not in courses:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    chain.reverse()
    return chain
