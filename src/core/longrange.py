"""Multi-semester projection: full-solve the next N terms on a scratch copy.

Given the current data, simulate forward term by term (FALL/SPRING only):

  1. recompute demand from each student's simulated progress (internal
     calculator — prereq-gated, requirement-driven),
  2. full CP-SAT solve for that term (same engine as the normal generator,
     including cadence gating, staffing pre-flight, and two-phase warm start),
  3. advance the world CAPACITY-LIMITED: students complete only the courses
     they actually got seats in (simulated grade "B"), credits and class
     standings update, finished students graduate out, and each fall a
     projected freshman cohort (FreshmenProjection) enters,
  4. repeat.

Everything runs against a THROWAWAY COPY of the database — the live data is
never touched. The product is the projection report (per-term summaries, a
course-by-term offering matrix, and risk flags like biennial courses skipped
while students still need them), not an operational timetable: run the
normal generator for the term you actually intend to publish.
"""

import logging
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from src.core.cadence import derive_standing, is_off_cadence, next_term
from src.core.conflicts import build_conflict_matrix
from src.core.demand import DemandCalculator
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.core.graph import PrerequisiteGraph
from src.core.scratchdb import backup_db
from src.core.timeutil import parse_semester
from src.database.connection import create_tables, get_engine
from src.database.models import (
    ClassStanding,
    Course,
    FreshmenProjection,
    Student,
    StudentCourseHistory,
    StudentDemand,
)

logger = logging.getLogger(__name__)

SIMULATED_GRADE = "B"  # passes any min-grade prerequisite rule
GRADUATION_CREDITS = 120


@dataclass
class CourseTermStat:
    sections: int = 0
    enrolled: int = 0
    demand: int = 0
    unmet: int = 0
    off_cadence_skipped: bool = False


@dataclass
class TermProjection:
    term: str
    status: str = "pending"
    sections: int = 0
    enrollments: int = 0
    demands: int = 0
    unmet: int = 0
    unmet_grad_critical: int = 0
    graduates: int = 0
    freshmen_added: int = 0
    students: int = 0
    solve_seconds: float = 0.0
    optimality_gap_pct: float | None = None
    staffing_warnings: list[str] = field(default_factory=list)
    by_course: dict[str, CourseTermStat] = field(default_factory=dict)


def _add_freshmen(session: Session, term: str) -> int:
    """Materialize the projected freshman cohort for a FALL term.

    Creates ordinary Student rows (NOT is_projected — inside the scratch
    copy they are full simulation participants who accumulate history and
    eventually graduate). FreshmenProjection rows apply when their semester
    matches the term or is blank (= every fall).
    """
    year, term_name = parse_semester(term)
    if term_name != "FALL":
        return 0
    rows = session.exec(
        select(FreshmenProjection).where(FreshmenProjection.is_active == True)  # noqa: E712
    ).all()
    created = 0
    for row in rows:
        if row.semester and row.semester.strip().upper() != term.upper():
            continue
        for i in range(row.estimated_count):
            sid = f"PROJ{term}{row.major}{i + 1:03d}"
            if session.get(Student, sid) is not None:
                continue
            session.add(Student(
                id=sid,
                name=f"Projected {row.major} {i + 1}",
                email="",
                class_standing=ClassStanding.FR,
                credits_earned=0,
                major=row.major,
                catalog_year=year,
            ))
            created += 1
    session.commit()
    return created


def _graduate_finished_students(
    session: Session, grad_credits: int
) -> int:
    """Remove students with no remaining demands and enough credits.

    Runs AFTER demand calculation: zero demand rows means the requirement
    engine found nothing left to take. Deleting them (scratch copy only!)
    keeps later terms' demand honest and gives the per-term graduate count.
    """
    demanded = {
        d.student_id for d in session.exec(select(StudentDemand)).all()
    }
    graduates = [
        s for s in session.exec(select(Student)).all()
        if s.id not in demanded
        and not s.is_projected
        and s.credits_earned >= grad_credits
    ]
    for s in graduates:
        session.delete(s)
    session.commit()
    return len(graduates)


def _advance_students(session: Session, result, term: str) -> int:
    """Capacity-limited progression: completed = actually seated."""
    section_course = {s.id: s.course_code for s in result.sections}
    courses = {c.code: c for c in session.exec(select(Course)).all()}
    students = {s.id: s for s in session.exec(select(Student)).all()}

    advanced = 0
    for e in result.enrollments:
        code = section_course.get(e.section_id)
        student = students.get(e.student_id)
        if code is None or student is None:
            continue
        credits = courses[code].credits if code in courses else 3
        session.add(StudentCourseHistory(
            student_id=student.id,
            course_code=code,
            grade=SIMULATED_GRADE,
            semester=term,
            credits_earned=credits,
        ))
        student.credits_earned += credits
        student.class_standing = derive_standing(student.credits_earned)
        session.add(student)
        advanced += 1
    session.commit()
    return advanced


def project_terms(
    source_db_path: Path | str,
    start_term: str,
    n_terms: int,
    config: SchedulerConfig | None = None,
    time_limit_per_term: int = 180,
    grad_credits: int = GRADUATION_CREDITS,
    emit: "Callable[[str], None] | None" = None,
    scratch_path: Path | str | None = None,
) -> tuple[list[TermProjection], Path]:
    """Simulate `n_terms` regular terms starting at `start_term`.

    Copies `source_db_path` to a scratch file (kept afterwards so the
    per-term schedules can be inspected; path is returned) and never writes
    to the source. Stops early if a term fails to solve — later terms would
    be fiction — and records the failure with its staffing warnings.
    """
    say = emit or (lambda m: None)
    source = Path(source_db_path)
    if not source.exists():
        raise FileNotFoundError(f"Source database not found: {source}")

    if scratch_path is None:
        fd = tempfile.NamedTemporaryFile(
            prefix="gradimize_projection_", suffix=".db", delete=False
        )
        scratch_path = Path(fd.name)
        fd.close()
    scratch_path = Path(scratch_path)
    backup_db(source, scratch_path)
    engine = get_engine(scratch_path)
    create_tables(engine)
    say(f"Projection scratch copy: {scratch_path}")

    if config is None:
        config = SchedulerConfig.load_default()
    config.solver_settings["max_time_seconds"] = time_limit_per_term
    # The projection must re-derive demand each simulated term from student
    # progress; a static registrations/audit snapshot cannot advance.
    config.demand_source = "internal"

    results: list[TermProjection] = []
    term = start_term
    for i in range(n_terms):
        tp = TermProjection(term=term)
        results.append(tp)
        say(f"[{i + 1}/{n_terms}] {term}: computing demand…")

        with Session(engine) as session:
            tp.freshmen_added = _add_freshmen(session, term)

            graph = PrerequisiteGraph(session)
            DemandCalculator(session, graph).calculate_all_demands()
            tp.graduates = _graduate_finished_students(session, grad_credits)

            demands = list(session.exec(select(StudentDemand)).all())
            tp.demands = len(demands)
            tp.students = len(session.exec(select(Student)).all())
            if not demands:
                tp.status = "empty"
                say(f"  {term}: no demand left — stopping.")
                break

            say(
                f"  {term}: {tp.students} students, {tp.demands} demands "
                f"(+{tp.freshmen_added} freshmen, -{tp.graduates} graduates); "
                f"solving…"
            )
            conflict_matrix = build_conflict_matrix(session)
            sched_engine = SchedulingEngine(session, conflict_matrix, config)
            sections = sched_engine.create_sections(term)
            result = sched_engine.solve(
                sections, f"Projection {term}", term
            )
            tp.status = result.status
            tp.staffing_warnings = list(result.staffing_warnings)
            tp.solve_seconds = result.solve_time_seconds
            if result.stats:
                tp.optimality_gap_pct = result.stats.get("optimality_gap_pct")

            if result.status not in ("optimal", "feasible"):
                say(f"  {term}: solve returned {result.status} — stopping the chain.")
                break

            tp.sections = len(result.sections)
            tp.enrollments = len(result.enrollments)

            # Per-course bookkeeping for the offering matrix.
            demand_by_course: dict[str, set[str]] = defaultdict(set)
            grad_critical_pairs = set()
            for d in demands:
                demand_by_course[d.course_code].add(d.student_id)
                if d.is_graduation_critical:
                    grad_critical_pairs.add((d.student_id, d.course_code))
            section_course = {s.id: s.course_code for s in result.sections}
            enrolled_pairs = {
                (e.student_id, section_course[e.section_id])
                for e in result.enrollments
                if e.section_id in section_course
            }
            sections_per_course: dict[str, int] = defaultdict(int)
            for s in result.sections:
                sections_per_course[s.course_code] += 1

            year, term_name = parse_semester(term)
            courses = {c.code: c for c in session.exec(select(Course)).all()}
            for code, students_wanting in demand_by_course.items():
                stat = CourseTermStat(
                    sections=sections_per_course.get(code, 0),
                    demand=len(students_wanting),
                    enrolled=sum(
                        1 for sid in students_wanting if (sid, code) in enrolled_pairs
                    ),
                )
                stat.unmet = stat.demand - stat.enrolled
                if stat.sections == 0 and code in courses:
                    stat.off_cadence_skipped = is_off_cadence(
                        courses[code], year, term_name
                    )
                tp.by_course[code] = stat

            tp.unmet = tp.demands - len(enrolled_pairs)
            tp.unmet_grad_critical = sum(
                1 for pair in grad_critical_pairs if pair not in enrolled_pairs
            )

            say(
                f"  {term}: {tp.status}, {tp.sections} sections, "
                f"{tp.enrollments}/{tp.demands} demands seated "
                f"({tp.unmet} unmet, {tp.unmet_grad_critical} grad-critical unmet)"
            )

            _advance_students(session, result, term)

        term = next_term(term)

    return results, scratch_path


def write_projection_report(
    results: list[TermProjection],
    path: Path | str,
    source_label: str = "",
) -> None:
    """Write the projection as a markdown report with an offering matrix."""
    terms = [tp.term for tp in results]
    lines = [
        f"# Multi-semester projection — {terms[0]} through {terms[-1]}",
        "",
        "Simulated with capacity-limited progression: students complete only",
        "the courses they actually got seats in, then next-term demand is",
        "re-derived from their (simulated) progress. Full solver run per term.",
        "Numbers get softer the further out the term — treat later columns as",
        "planning signals, not commitments.",
        "",
        "**Reading \"unmet\":** demand here is every course a student still",
        "needs toward their degree — a full multi-term backlog, not one term's",
        "course load (a student only takes ~5 courses a term). Large unmet",
        "numbers are backlog, not students turned away. The signals that",
        "matter: **grad-critical unmet** (should be 0), the **term-over-term",
        "trend** (backlog should drain), and matrix cells whose demand GROWS",
        "across terms — that course is a capacity bottleneck.",
        "",
    ]
    if source_label:
        lines += [f"- Source data: {source_label}"]
    lines += [
        f"- Terms simulated: {len(results)}",
        "",
        "## Term summaries",
        "",
        "| Term | Status | Students | Sections | Seated / Demands | Unmet "
        "| Grad-critical unmet | Graduates out | Freshmen in |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for tp in results:
        lines.append(
            f"| {tp.term} | {tp.status} | {tp.students} | {tp.sections} "
            f"| {tp.enrollments} / {tp.demands} | {tp.unmet} "
            f"| {tp.unmet_grad_critical} | {tp.graduates} | {tp.freshmen_added} |"
        )

    all_courses = sorted({c for tp in results for c in tp.by_course})
    if all_courses:
        lines += [
            "",
            "## Offering matrix",
            "",
            "Cells: `sections × (enrolled/demand)`; `!` = unmet demand; "
            "`skip` = off-cadence term with demand present.",
            "",
            "| Course | " + " | ".join(terms) + " |",
            "|---|" + "---|" * len(terms),
        ]
        for code in all_courses:
            cells = []
            for tp in results:
                stat = tp.by_course.get(code)
                if stat is None:
                    cells.append("·")
                elif stat.sections == 0:
                    marker = "skip" if stat.off_cadence_skipped else "0"
                    cells.append(f"{marker}! ({stat.demand} want)")
                else:
                    bang = "!" if stat.unmet else ""
                    cells.append(
                        f"{stat.sections}×({stat.enrolled}/{stat.demand}){bang}"
                    )
            lines.append(f"| {code} | " + " | ".join(cells) + " |")

    flags: list[str] = []
    for tp in results:
        for w in tp.staffing_warnings:
            flags.append(f"{tp.term}: {w}")
        for code, stat in sorted(tp.by_course.items()):
            if stat.off_cadence_skipped and stat.demand:
                flags.append(
                    f"{tp.term}: {code} is off-cadence this term but "
                    f"{stat.demand} student(s) need it — check whether the "
                    f"cohort can wait for the next offering"
                )
        if tp.status not in ("optimal", "feasible", "pending", "empty"):
            flags.append(
                f"{tp.term}: solve returned {tp.status} — projection stopped here"
            )
    lines += ["", "## Risk flags", ""]
    lines += [f"- {f}" for f in flags] if flags else ["- none"]
    lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")
