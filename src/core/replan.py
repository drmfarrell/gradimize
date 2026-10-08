"""One-student replanner: fastest remaining path to graduation, term by term.

Given a single student who has fallen off their normal sequence (failed a
prerequisite, needs a biennial course that just went off-cadence, whatever),
simulate their remaining coursework forward term by term against the
catalog's offering patterns — cadence and `offer_terms`, not staffing or
seats — greedily filling each term up to a credit cap with whatever the
requirement engine says is most urgent and is actually offered that term.

This module reuses `DemandCalculator.calculate_student_demand` for ALL
requirement-group, attribute, and prerequisite logic; it does not
reimplement any of that. It only adds the term-by-term SEQUENCING layer on
top: what's offered THIS term, how much fits in a course load, and what a
missed off-cadence course costs in wait time.

Like `longrange.py`'s multi-student projection, this runs against a
THROWAWAY SQLite copy of the database (online-backup API — never a plain
file copy, and never a write to the source). Unlike `longrange.py`, this is
single-student and purely catalog-level: no CP-SAT solve, no room/faculty/
section modeling, no seat limits. Capacity is NOT simulated — seats are
assumed to be available whenever a course is offered. Cross-check the
term's actual generated schedule before treating this plan as a promise to
a student (see the footer `render_replan_markdown` appends to every plan).
"""

import tempfile
from pathlib import Path

from sqlmodel import Session, select

from src.core.cadence import derive_standing, is_off_cadence, next_term
from src.core.demand import DemandCalculator
from src.core.graph import PrerequisiteGraph
from src.core.scratchdb import backup_db, delete_scratch
from src.core.timeutil import parse_semester
from src.database.connection import create_tables, get_engine
from src.database.models import Course, Student, StudentCourseHistory, StudentDemand

SIMULATED_GRADE = "B"  # passes any min-grade prerequisite rule
CADENCE_HORIZON_TERMS = 8  # ~4 years — how far ahead to look for a course's next offering


def _next_offered_term(
    course: Course, from_term: str, horizon: int = CADENCE_HORIZON_TERMS
) -> tuple[str | None, int | None]:
    """Find when `course` is next offered, searching from `from_term` (inclusive).

    Walks `next_term()` up to `horizon` hops looking for the first term in
    which the course is not off-cadence.

    Returns:
        (term, steps) where `steps` is the number of `next_term()` hops from
        `from_term` needed to reach it (0 if `from_term` itself qualifies),
        or (None, None) if nothing turns up within `horizon` hops.
    """
    term = from_term
    for step in range(horizon + 1):
        year, term_name = parse_semester(term)
        if not is_off_cadence(course, year, term_name):
            return term, step
        term = next_term(term)
    return None, None


def _refresh_demand(session: Session, student_id: str) -> tuple[Student, list[StudentDemand]]:
    """Refetch the student and recompute demand against current history.

    Rebuilds the prerequisite graph and demand calculator every call — the
    student's simulated history changes between terms, and demand depends
    on it (available courses, credits/standing-driven urgency).

    Raises:
        ValueError: if `student_id` does not exist in this session.
    """
    student = session.get(Student, student_id)
    if student is None:
        raise ValueError(f"Student {student_id!r} not found")
    graph = PrerequisiteGraph(session)
    calc = DemandCalculator(session, graph)
    return student, calc.calculate_student_demand(student)


def replan_student(
    source_db: Path,
    student_id: str,
    start_term: str = "2026FA",
    max_terms: int = 12,
    credits_per_term: int = 15,
) -> dict:
    """Simulate one student's fastest remaining path to graduation.

    Copies `source_db` to a scratch file via the SQLite online-backup API,
    simulates forward from `start_term` for up to `max_terms` regular terms,
    and always deletes the scratch copy before returning (unlike
    `longrange.project_terms`, nothing here is meant to be inspected
    afterward — the return value IS the report). The source database is
    never opened for writing.

    Each term: recompute demand, keep only courses actually offered this
    term (cadence/`offer_terms`), greedily take the highest-urgency ones
    that fit within `credits_per_term`, and simulate completion (grade "B")
    for whatever was taken. Stops early once the student has no remaining
    demand (graduation) or after `max_terms` terms, whichever comes first.

    Args:
        source_db: Path to the source SQLite database (read-only).
        student_id: The student to plan for.
        start_term: First regular term to plan, e.g. "2026FA".
        max_terms: Maximum number of regular terms to simulate.
        credits_per_term: Credit-load cap per simulated term.

    Returns:
        A dict:
            student_id, student_name, major, start_term, starting_credits,
            terms: [{"term", "courses": [{"code","title","credits","urgency",
                "reason"}], "credits"}],
            graduates_after: the last term simulated if requirements were
                completed, else None,
            projected_credits: credits_earned at the end of the simulation,
            remaining: [{"code","next_offered"}] — populated only when
                `max_terms` ran out before requirements were complete,
            warnings: off-cadence "you'll wait a long time" notices,
            already_complete: True if the student had zero demand before
                any term was simulated (nothing to plan).

    Raises:
        FileNotFoundError: if `source_db` does not exist.
        ValueError: if `student_id` does not exist in the database.
    """
    source = Path(source_db)
    if not source.exists():
        raise FileNotFoundError(f"Source database not found: {source}")

    fd = tempfile.NamedTemporaryFile(prefix="gradimize_replan_", suffix=".db", delete=False)
    scratch_path = Path(fd.name)
    fd.close()

    engine = None
    try:
        backup_db(source, scratch_path)
        engine = get_engine(scratch_path)
        create_tables(engine)

        terms_out: list[dict] = []
        warnings: list[str] = []
        warned_courses: set[str] = set()
        graduates_after: str | None = None
        already_complete = False
        remaining: list[dict] = []

        with Session(engine) as session:
            course_map = {c.code: c for c in session.exec(select(Course)).all()}

            # Raises ValueError here if student_id doesn't exist.
            student, demands = _refresh_demand(session, student_id)
            student_name = student.name
            major = student.major
            starting_credits = student.credits_earned

            term = start_term
            for _ in range(max_terms):
                if not demands:
                    if terms_out:
                        graduates_after = terms_out[-1]["term"]
                    else:
                        already_complete = True
                    break

                year, term_name = parse_semester(term)
                offered: list[StudentDemand] = []
                not_offered: list[StudentDemand] = []
                for d in demands:
                    course = course_map.get(d.course_code)
                    if course is None:
                        continue  # StudentDemand outlived its Course row somehow
                    if is_off_cadence(course, year, term_name):
                        not_offered.append(d)
                    else:
                        offered.append(d)

                # Highest urgency first; course_code only breaks ties so our
                # OWN output is deterministic regardless of set-iteration
                # order upstream in calculate_student_demand.
                offered.sort(key=lambda d: (-d.urgency_score, d.course_code))

                taken: list[tuple[StudentDemand, Course]] = []
                total_credits = 0
                for d in offered:
                    course = course_map[d.course_code]
                    if total_credits + course.credits <= credits_per_term:
                        taken.append((d, course))
                        total_credits += course.credits
                    # else: would overflow this term — skip it, keep
                    # scanning for something smaller that still fits.

                for d in sorted(not_offered, key=lambda d: d.course_code):
                    code = d.course_code
                    if code in warned_courses:
                        continue
                    course = course_map[code]
                    next_avail, steps = _next_offered_term(course, term)
                    if steps is not None and steps >= 2:
                        warnings.append(
                            f"{code} not offered again until {next_avail} — "
                            "a missed term costs a year"
                        )
                        warned_courses.add(code)
                    elif next_avail is None:
                        warnings.append(
                            f"{code}: not offered again within the next "
                            f"{CADENCE_HORIZON_TERMS} terms after {term} — "
                            "check its cadence configuration"
                        )
                        warned_courses.add(code)

                terms_out.append({
                    "term": term,
                    "courses": [
                        {
                            "code": course.code,
                            "title": course.title,
                            "credits": course.credits,
                            "urgency": d.urgency_score,
                            "reason": d.reason,
                        }
                        for d, course in taken
                    ],
                    "credits": total_credits,
                })

                for d, course in taken:
                    session.add(StudentCourseHistory(
                        student_id=student_id,
                        course_code=course.code,
                        grade=SIMULATED_GRADE,
                        semester=term,
                        credits_earned=course.credits,
                    ))
                student.credits_earned += total_credits
                student.class_standing = derive_standing(student.credits_earned)
                session.add(student)
                session.commit()

                term = next_term(term)
                student, demands = _refresh_demand(session, student_id)

            # Loop ended either via the `break` above (graduation detected
            # mid-loop) or by exhausting max_terms. Either way, `demands`/
            # `term` now hold the freshest post-simulation snapshot: if the
            # loop merely ran out of terms right as the last course landed,
            # this still resolves as graduation rather than false "remaining".
            if not already_complete and graduates_after is None:
                if not demands:
                    if terms_out:
                        graduates_after = terms_out[-1]["term"]
                    else:
                        already_complete = True
                else:
                    for d in sorted(demands, key=lambda d: (-d.urgency_score, d.course_code)):
                        course = course_map.get(d.course_code)
                        if course is None:
                            continue
                        next_avail, _steps = _next_offered_term(course, term)
                        remaining.append({"code": d.course_code, "next_offered": next_avail})

            projected_credits = student.credits_earned

        return {
            "student_id": student_id,
            "student_name": student_name,
            "major": major,
            "start_term": start_term,
            "starting_credits": starting_credits,
            "terms": terms_out,
            "graduates_after": graduates_after,
            "projected_credits": projected_credits,
            "remaining": remaining,
            "warnings": warnings,
            "already_complete": already_complete,
        }
    finally:
        if engine is not None:
            engine.dispose()
        delete_scratch(scratch_path)


def render_replan_markdown(plan: dict) -> str:
    """Render a `replan_student` plan as a compact advising handout.

    Deterministic (no timestamps): the same plan dict always renders to the
    same text.
    """
    lines = [
        f"# Replan: {plan['student_name']} ({plan['student_id']})",
        "",
        f"- Major: {plan['major']}",
        f"- Starting credits: {plan['starting_credits']}",
        f"- Plan starts: {plan['start_term']}",
        "",
    ]

    if plan.get("already_complete"):
        lines += [
            "## Outcome",
            "",
            f"{plan['student_name']} has already satisfied every degree "
            "requirement the catalog can see — nothing left to plan.",
            "",
        ]
    else:
        for t in plan["terms"]:
            lines.append(f"## {t['term']}")
            lines.append("")
            if t["courses"]:
                lines.append("| Code | Title | Credits |")
                lines.append("|---|---|---:|")
                for c in t["courses"]:
                    lines.append(f"| {c['code']} | {c['title']} | {c['credits']} |")
            else:
                lines.append("_Nothing fits this term (needed courses are off-cadence)._")
            lines.append("")
            lines.append(f"Term credits: {t['credits']}")
            lines.append("")

        lines.append("## Outcome")
        lines.append("")
        n = len(plan["terms"])
        if plan["graduates_after"] is not None:
            term_word = "term" if n == 1 else "terms"
            lines.append(
                f"On this plan, requirements complete after "
                f"{plan['graduates_after']} — {n} more regular {term_word}."
            )
        else:
            term_word = "term" if n == 1 else "terms"
            lines.append(
                f"Plan horizon ({n} {term_word}) reached with requirements "
                "still outstanding."
            )
            if plan["remaining"]:
                lines.append("")
                lines.append("### Remaining requirements")
                lines.append("")
                lines.append("| Code | Next offered |")
                lines.append("|---|---|")
                for r in plan["remaining"]:
                    nxt = r["next_offered"] if r["next_offered"] else "unknown"
                    lines.append(f"| {r['code']} | {nxt} |")
        lines.append("")

    if plan["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for w in plan["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("---")
    lines.append(
        "Capacity not simulated — cross-check the term's actual schedule "
        "before relying on this plan."
    )
    lines.append("")

    return "\n".join(lines)
