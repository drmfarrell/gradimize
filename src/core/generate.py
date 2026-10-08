"""Schedule generation with progress callbacks.

Wraps the full generate pipeline (demand calc → conflict matrix → section
creation → solve) behind a single `emit`-driven function so it can be
streamed to the web UI via Server-Sent Events, or called headless.
"""

import logging
import re
from typing import Callable

from sqlmodel import Session, func, select

from src.core.conflicts import build_conflict_matrix
from src.core.demand import DemandCalculator, ProjectionDemandCalculator
from src.core.engine import SchedulerConfig, SchedulingEngine
from src.core.graph import PrerequisiteGraph
from src.database.connection import get_engine
from src.database.models import ScheduleVersion, Section, StudentDemand

logger = logging.getLogger(__name__)

# Strip Rich/Textual markup tags (e.g. [dim], [/green bold]) that the solver
# emits for the TUI, so web log lines are clean.
# Lowercase-only: Rich style tags are lowercase; keep [WARN]-style text.
_MARKUP = re.compile(r"\[/?[a-z ]+\]")


def _clean(line: str) -> str:
    return _MARKUP.sub("", line).rstrip()


# emit(event_type, **fields) — event_type is one of: "log", "done", "error".
Emit = Callable[..., None]


def _teed(emit: Emit, runlog) -> Emit:
    """Wrap `emit` so every stream event is also recorded in the run log.

    Solve-stream events are student-data-free by construction (sections,
    rooms, slots, counts, CP-SAT stats) — verbatim is safe, and the canary
    test enforces it. The one exception: "error" events, whose messages may
    quote exception payloads, get scrub_text()'d in the log copy. The
    wrapper must never break the stream, so log failures are swallowed
    (RunLog also swallows internally; belt and suspenders — this stream
    feeds a live solve).
    """
    from src.core.runlog import scrub_text

    def wrapped(event_type: str, **fields) -> None:
        try:
            if event_type == "error" and "message" in fields:
                runlog.event("error", message=scrub_text(str(fields["message"])))
            else:
                runlog.event(event_type, **fields)
        except Exception:
            pass
        emit(event_type, **fields)

    return wrapped


def bootstrap_demands(session: Session, config: SchedulerConfig, log) -> None:
    """Calculate demands using the configured source.

    Uses the DegreeWorks audit CSV when config.demand_source == "degreeworks"
    and the file exists, else the internal DemandCalculator. This is the ONE
    home for the source-fallback logic (the TUI dashboard, run_solver.py, and
    the retrospective sim each used to carry a diverging copy).

    Args:
        session: DB session.
        config: Scheduler config (for demand_source / degreeworks_csv).
        log: Callable receiving progress strings.
    """
    from pathlib import Path

    graph = PrerequisiteGraph(session)

    if config.demand_source == "degreeworks" and config.degreeworks_csv:
        dw_path = Path(config.degreeworks_csv)
        if dw_path.exists():
            from src.core.degreeworks import DegreeWorksImporter

            log(f"Importing DegreeWorks audit: {dw_path.name}")
            importer = DegreeWorksImporter(session, graph)
            result = importer.import_audit_needs(dw_path)
            log(
                f"DegreeWorks: {result['demands_created']} demands for "
                f"{result['students']} students"
            )
            return
        log(f"DegreeWorks CSV not found: {dw_path} — using internal calculator")

    log("Calculating demands (internal)…")
    created = DemandCalculator(session, graph).calculate_all_demands()
    log(f"Calculated {created} student demands.")


def _failure_message(result) -> str:
    """Actionable failure text: names the conflicting constraint families
    (with hints) for INFEASIBLE, or suggests more time for UNKNOWN."""
    from src.core.engine import INFEASIBILITY_HINTS

    if result.status == "unknown":
        return ("Solver returned status: unknown — no solution found in the "
                "time limit; try increasing it")
    msg = f"Solver returned status: {result.status}"
    core = getattr(result, "infeasible_core", None)
    if core:
        msg += ". Conflicting requirements: " + ", ".join(core) + "."
        hints = [INFEASIBILITY_HINTS[f] for f in core if f in INFEASIBILITY_HINTS]
        if hints:
            msg += " " + " ".join(hints)
    return msg


def run_generation(
    emit: Emit,
    semester: str = "2025SP",
    name: str = "Generated Schedule",
    workers: int = 0,
    time_limit: int = 120,
    include_projections: bool = False,
    class_size_cap: int | None = None,
    capacity_mode: str | None = None,
    conflict_threshold: float | None = None,
) -> None:
    """Run the schedule generation pipeline, reporting progress via `emit`.

    Emits "log" events throughout, then exactly one terminal event:
    "done" (with schedule_id and stats) or "error" (with message).
    Uses its own DB session so it is safe to run in a worker thread.

    When `include_projections` is True, synthetic demand for the projected
    incoming freshman class (from active FreshmenProjection rows) is folded
    into the solve so intro courses are sized for students who aren't in
    Banner yet. When False, any stale projected demand from a prior run is
    cleared so the schedule reflects only enrolled students.

    `class_size_cap` bounds every class at N seats for this run (0/None =
    config default); `capacity_mode` "room" lets section sizes float up to
    the assigned room's capacity (see SchedulerConfig.section_settings).
    """
    from src.core.runlog import RunLog

    runlog = RunLog("solve")
    emit = _teed(emit, runlog)
    runlog.event("params", semester=semester, name=name, workers=workers,
                 time_limit=time_limit, include_projections=include_projections,
                 class_size_cap=class_size_cap, capacity_mode=capacity_mode,
                 conflict_threshold=conflict_threshold)
    try:
        with Session(get_engine()) as session:
            graph = PrerequisiteGraph(session)
            config = SchedulerConfig.load_default()
            config.solver_settings["max_time_seconds"] = time_limit
            config.solver_settings["num_workers"] = workers
            # Per-run class-size overrides (None/0 = keep YAML/config values).
            # The cap sets both the fixed-mode section size and the room-mode
            # ceiling — one user intent, "no class bigger than N".
            if class_size_cap:
                config.section_settings["default_capacity"] = class_size_cap
                config.section_settings["max_class_size"] = class_size_cap
            if capacity_mode:
                config.section_settings["capacity_mode"] = capacity_mode
            # Lower = stricter about student conflicts (more course pairs
            # penalized). Small cohorts can afford stricter values — pair
            # urgency sums are small, so the default 50 ignores real pairs.
            if conflict_threshold is not None:
                config.conflict_urgency_threshold = conflict_threshold

            # Decide real-demand calc on real (non-projected) demands only, so
            # leftover projected rows from a previous run don't suppress it.
            n_real = session.exec(
                select(func.count()).select_from(StudentDemand).where(
                    StudentDemand.is_projected == False  # noqa: E712
                )
            ).one()
            if n_real == 0:
                emit("log", msg="No student demands found — calculating…")
                bootstrap_demands(session, config, lambda m: emit("log", msg=m))
            else:
                emit("log", msg=f"Using {n_real} existing student demands.")

            proj_calc = ProjectionDemandCalculator(session, graph)
            if include_projections:
                pres = proj_calc.generate_projected_demand()
                if pres["students_created"]:
                    emit("log", msg=(
                        f"Added {pres['students_created']} projected freshmen "
                        f"({pres['demands_created']} demand records) from active "
                        f"projections."
                    ))
                else:
                    emit("log", msg=(
                        "Include-projections is on, but no active freshmen "
                        "projections were found."
                    ))
            else:
                cleared = proj_calc.clear_projected_data()
                if cleared["students_deleted"]:
                    emit("log", msg=(
                        f"Excluding projections — cleared "
                        f"{cleared['students_deleted']} stale projected students."
                    ))

            emit("log", msg="Building conflict matrix…")
            conflict_matrix = build_conflict_matrix(session)
            emit("log", msg=f"Solver: {workers or 'auto'} workers, {time_limit}s limit.")

            engine = SchedulingEngine(session, conflict_matrix, config)
            sections = engine.create_sections(semester)
            emit("log", msg=f"Created {len(sections)} sections to schedule.")

            result = engine.solve(
                sections,
                name,
                semester,
                log_callback=lambda line: emit("log", msg=_clean(line)),
                viz_emit=emit,
            )

            if result.status in ("optimal", "feasible"):
                schedule_id = result.sections[0].schedule_id if result.sections else None
                emit(
                    "done",
                    schedule_id=schedule_id,
                    status=result.status,
                    sections_count=len(result.sections),
                    enrollments_count=len(result.enrollments),
                    objective_value=result.objective_value,
                    solve_time_seconds=result.solve_time_seconds,
                )
            else:
                emit("error", message=_failure_message(result))
    except Exception as e:  # noqa: BLE001 — surface any failure to the client
        logger.exception("Schedule generation failed")
        runlog.exception("run_generation", e)
        emit("error", message=f"{type(e).__name__}: {e}")
    finally:
        runlog.close()


def run_resolve(
    emit: Emit,
    schedule_id: int,
    name: str | None = None,
    workers: int = 0,
    time_limit: int = 120,
) -> None:
    """Re-solve an existing schedule, preserving pinned sections, via `emit`.

    Pinned sections keep their slot/room/faculty; everything else is
    re-optimized. Writes a NEW ScheduleVersion. Same event protocol as
    run_generation. Safe to run in a worker thread (own DB session).
    """
    from src.core.runlog import RunLog

    runlog = RunLog("resolve")
    emit = _teed(emit, runlog)
    runlog.event("params", schedule_id=schedule_id, name=name,
                 workers=workers, time_limit=time_limit)
    try:
        with Session(get_engine()) as session:
            original = session.get(ScheduleVersion, schedule_id)
            if original is None:
                emit("error", message=f"Schedule {schedule_id} not found")
                return

            sections = list(
                session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
            )
            if not sections:
                emit("error", message="Schedule has no sections to re-solve")
                return
            n_pinned = sum(1 for s in sections if s.is_pinned)
            emit(
                "log",
                msg=(
                    f"Re-solving '{original.name}': {n_pinned} pinned, "
                    f"{len(sections) - n_pinned} to re-optimize."
                ),
            )

            emit("log", msg="Building conflict matrix…")
            conflict_matrix = build_conflict_matrix(session)

            config = SchedulerConfig.load_default()
            config.solver_settings["max_time_seconds"] = time_limit
            config.solver_settings["num_workers"] = workers
            emit("log", msg=f"Solver: {workers or 'auto'} workers, {time_limit}s limit.")

            engine = SchedulingEngine(session, conflict_matrix, config)
            result = engine.re_solve(
                schedule_id,
                name,
                log_callback=lambda line: emit("log", msg=_clean(line)),
                viz_emit=emit,
            )

            if result.status in ("optimal", "feasible"):
                new_id = result.sections[0].schedule_id if result.sections else None
                emit(
                    "done",
                    schedule_id=new_id,
                    status=result.status,
                    sections_count=len(result.sections),
                    enrollments_count=len(result.enrollments),
                    objective_value=result.objective_value,
                    solve_time_seconds=result.solve_time_seconds,
                )
            else:
                emit("error", message=_failure_message(result))
    except Exception as e:  # noqa: BLE001
        logger.exception("Re-solve failed")
        runlog.exception("run_resolve", e)
        emit("error", message=f"{type(e).__name__}: {e}")
    finally:
        runlog.close()


def run_repair(
    emit: Emit,
    schedule_id: int,
    name: str | None = None,
    workers: int = 0,
    time_limit: int = 120,
) -> None:
    """Minimal-change repair of a published schedule, via `emit`.

    Re-solves against CURRENT data (a resigned instructor, a deactivated
    room, changed approvals) with every section anchored to its published
    slot/room/instructor/roster — only changes the disruption forces will
    happen. Writes a NEW ScheduleVersion; the base is untouched. The "done"
    event includes `repair_diff` (what moved vs the base) and each move is
    also emitted as a log line. Same event protocol as run_generation.
    """
    from src.core.runlog import RunLog
    from src.core.schedule_diff import diff_schedules

    runlog = RunLog("repair")
    emit = _teed(emit, runlog)
    runlog.event("params", schedule_id=schedule_id, name=name,
                 workers=workers, time_limit=time_limit)
    try:
        with Session(get_engine()) as session:
            original = session.get(ScheduleVersion, schedule_id)
            if original is None:
                emit("error", message=f"Schedule {schedule_id} not found")
                return

            sections = list(
                session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
            )
            if not sections:
                emit("error", message="Schedule has no sections to repair")
                return
            n_pinned = sum(1 for s in sections if s.is_pinned)
            emit(
                "log",
                msg=(
                    f"Repair mode: anchoring {len(sections)} published sections "
                    f"({n_pinned} pinned) from '{original.name}' — only forced "
                    f"changes will move."
                ),
            )

            emit("log", msg="Building conflict matrix…")
            conflict_matrix = build_conflict_matrix(session)

            config = SchedulerConfig.load_default()
            config.solver_settings["max_time_seconds"] = time_limit
            config.solver_settings["num_workers"] = workers
            emit("log", msg=f"Solver: {workers or 'auto'} workers, {time_limit}s limit.")

            engine = SchedulingEngine(session, conflict_matrix, config)
            result = engine.repair_solve(
                schedule_id,
                name,
                log_callback=lambda line: emit("log", msg=_clean(line)),
                viz_emit=emit,
            )

            if result.status in ("optimal", "feasible"):
                new_id = result.sections[0].schedule_id if result.sections else None
                diff = None
                if new_id is not None:
                    diff = diff_schedules(session, new_id, base_id=schedule_id)
                    for c in diff["changed"]:
                        moves = "; ".join(
                            f"{field}: {d['from'] or '—'} → {d['to'] or '—'}"
                            for field, d in c["changes"].items()
                        )
                        emit("log", msg=f"MOVED {c['course_code']}-{c['section_number']}: {moves}")
                    for r in diff["removed"]:
                        emit("log", msg=(
                            f"DROPPED {r['course_code']}-{r['section_number']} "
                            f"(closed or no longer schedulable)"
                        ))
                    emit("log", msg=(
                        f"Repair result: {diff['unchanged_count']} sections untouched, "
                        f"{len(diff['changed'])} changed, {len(diff['removed'])} dropped."
                    ))
                emit(
                    "done",
                    schedule_id=new_id,
                    status=result.status,
                    sections_count=len(result.sections),
                    enrollments_count=len(result.enrollments),
                    objective_value=result.objective_value,
                    solve_time_seconds=result.solve_time_seconds,
                    repair_diff=diff,
                )
            else:
                emit("error", message=_failure_message(result))
    except Exception as e:  # noqa: BLE001
        logger.exception("Repair failed")
        runlog.exception("run_repair", e)
        emit("error", message=f"{type(e).__name__}: {e}")
    finally:
        runlog.close()
