"""Enrollment stress test: how does next term's schedule hold up if the
incoming freshman class comes in bigger or smaller than projected?

For each scaling factor F (0.8 = 20% smaller, 1.2 = 20% bigger, ...), a
THROWAWAY scratch copy of the source database is made (sqlite3 online-backup
API — see src/core/scenarios.py / src/core/longrange.py for why never
shutil: a WAL-mode database's uncommitted writes live in a -wal sidecar that
a plain file copy would silently drop). Inside that copy, every ACTIVE
FreshmenProjection.estimated_count is scaled by F (deterministically
rounded — no randomness anywhere), projected demand is regenerated from the
scaled counts (src.core.demand.ProjectionDemandCalculator), and the SAME
full CP-SAT solve (same time budget, same engine, same config) runs as for
any other factor. Real (returning-student) demand is never touched — it is
identical in every scratch copy because it is identical in the source, so
any difference between variants is attributable to the freshman class size
alone. The source database is never opened for writing.

The product is a fragility report: which courses would be undersized by a
smaller-than-projected class ("fragile-low": a section drops below the
course's minimum enrollment) and which would overflow a bigger one
("fragile-high": demand outstrips the seats on offer), relative to how that
same course behaves at the baseline (factor 1.0) projection. A factor whose
solve fails outright (infeasible/unknown) is itself a top-line finding, not
a crash — that class size does not fit the current room/instructor supply
at all.
"""

import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from src.core.conflicts import build_conflict_matrix
from src.core.demand import DemandCalculator, ProjectionDemandCalculator
from src.core.engine import SchedulerConfig, SchedulingEngine, SolverResult
from src.core.graph import PrerequisiteGraph
from src.core.scenarios import compute_scenario_metrics, format_metric_value
from src.core.scratchdb import delete_scratch, scratch_copy
from src.database.connection import create_tables, get_engine
from src.database.models import (
    Course,
    FreshmenProjection,
    Room,
    StudentDemand,
    TimeSlot,
)

logger = logging.getLogger(__name__)

DEFAULT_FACTORS: tuple[float, ...] = (0.8, 0.9, 1.0, 1.1, 1.2)
BASELINE_FACTOR = 1.0
OK_STATUSES = ("optimal", "feasible")

# Rows shown in the side-by-side markdown table: scenarios.py's metric set
# (sections, enrollment, demand coverage, grad-critical, room utilization)
# plus the stress-specific additions computed on top (status, unmet
# grad-critical, sections below minimum, staffing warnings, solve time).
METRIC_ROWS: list[tuple[str, str]] = [
    ("Status", "status"),
    ("Sections scheduled", "sections"),
    ("Total enrollment", "total_enrollment"),
    ("Projected freshmen modeled", "projected_students"),
    ("Demands with offered sections", "demands_with_sections"),
    ("Total demands", "demands_total"),
    ("Grad-critical demands", "grad_critical_total"),
    ("Grad-critical unmet", "unmet_grad_critical"),
    ("Sections below min enrollment", "sections_below_min_count"),
    ("Staffing warnings", "staffing_warnings_count"),
    ("Room utilization %", "room_util_pct"),
    ("Solve time (s)", "solve_seconds"),
]


def _count_active_projections(source: Path) -> int:
    """How many active FreshmenProjection rows the source has.

    Answered from a disposable scratch copy (backup API, then discarded) so
    even this check never opens the source for writing, and a source with
    nothing to stress never pays for a single scratch copy per factor.
    """
    scratch = scratch_copy(source, "stress_precheck")
    try:
        engine = get_engine(scratch)
        create_tables(engine)
        with Session(engine) as session:
            rows = session.exec(
                select(FreshmenProjection).where(
                    FreshmenProjection.is_active == True  # noqa: E712
                )
            ).all()
            return len(rows)
    finally:
        delete_scratch(scratch)


def _scale_projections(session: Session, factor: float) -> int:
    """Scale every ACTIVE FreshmenProjection.estimated_count by `factor`.

    Deterministic: max(0, round(count * factor)) — no randomness anywhere,
    so re-running the same factor always reproduces the same schedule.
    Returns the number of rows scaled.
    """
    rows = session.exec(
        select(FreshmenProjection).where(FreshmenProjection.is_active == True)  # noqa: E712
    ).all()
    for row in rows:
        row.estimated_count = max(0, round(row.estimated_count * factor))
        session.add(row)
    session.commit()
    return len(rows)


def _course_stats(
    demands: list[StudentDemand],
    result: SolverResult,
    courses: dict[str, Course],
) -> dict[str, dict]:
    """Per-course demand/enrolled/min-enrollment breakdown for one variant.

    Deliberately left EMPTY by the caller when the solve failed (see
    _solve_variant): a failed solve's result.sections/enrollments are empty
    by construction, which would otherwise make every course with any
    demand look like a capacity overflow ("unmet == full demand") — a
    solver failure is a top-line finding on its own, not a per-course
    fragility signal.
    """
    demand_by_course: dict[str, int] = defaultdict(int)
    for d in demands:
        demand_by_course[d.course_code] += 1

    enrolled_by_section: dict[int, int] = defaultdict(int)
    for e in result.enrollments:
        enrolled_by_section[e.section_id] += 1

    sections_by_course: dict[str, list] = defaultdict(list)
    for s in result.sections:
        sections_by_course[s.course_code].append(s)

    stats: dict[str, dict] = {}
    for code, demand_n in demand_by_course.items():
        course = courses.get(code)
        min_enrollment = course.min_enrollment if course else 5
        sections = sections_by_course.get(code, [])
        enrolled = sum(enrolled_by_section.get(s.id, 0) for s in sections)
        below_min_sections = [
            {
                "crn": s.crn,
                "section_number": s.section_number,
                "enrolled": enrolled_by_section.get(s.id, 0),
                "min_enrollment": min_enrollment,
            }
            for s in sections
            if enrolled_by_section.get(s.id, 0) < min_enrollment
        ]
        stats[code] = {
            "demand": demand_n,
            "enrolled": enrolled,
            "sections_count": len(sections),
            "min_enrollment": min_enrollment,
            "below_min": bool(below_min_sections),
            "below_min_sections": below_min_sections,
            "unmet": max(0, demand_n - enrolled),
        }
    return stats


def _solve_variant(
    scratch_path: Path,
    semester: str,
    factor: float,
    config: SchedulerConfig,
    log: Callable[[str], None],
) -> dict:
    """Scale, regenerate projected demand, solve, and compute one variant.

    Mirrors src.core.scenarios.solve_copy's shape (demand fallback,
    conflict matrix, create_sections, solve, compute_scenario_metrics),
    with the freshmen-projection scaling step spliced in first and the
    stress-specific metrics (unmet grad-critical, sections below minimum,
    per-course breakdown) layered on top.
    """
    engine = get_engine(scratch_path)
    create_tables(engine)
    with Session(engine) as session:
        n_scaled = _scale_projections(session, factor)
        log(f"  scaled {n_scaled} active freshmen projection row(s) by {factor}x")

        graph = PrerequisiteGraph(session)
        proj_result = ProjectionDemandCalculator(session, graph).generate_projected_demand()
        log(
            f"  projected demand: {proj_result['students_created']} synthetic "
            f"freshmen, {proj_result['demands_created']} demand records"
        )

        demands = list(session.exec(select(StudentDemand)).all())
        if not demands:
            log("  no demand rows found — computing via internal DemandCalculator…")
            DemandCalculator(session, graph).calculate_all_demands()
            demands = list(session.exec(select(StudentDemand)).all())

        conflict_matrix = build_conflict_matrix(session)
        sched_engine = SchedulingEngine(session, conflict_matrix, config)
        sections = sched_engine.create_sections(semester)
        result = sched_engine.solve(sections, f"Stress {factor}x {semester}", semester)
        log(
            f"  factor {factor}x: {result.status} ({len(result.sections)} sections, "
            f"{len(result.enrollments)} enrolled) in {result.solve_time_seconds:.1f}s"
        )

        rooms = list(session.exec(select(Room)).all())
        time_slots = list(session.exec(select(TimeSlot)).all())
        base_metrics = compute_scenario_metrics(
            result, demands, rooms, time_slots, sched_engine.slot_overlaps
        )

        courses = {c.code: c for c in session.exec(select(Course)).all()}
        by_course = (
            _course_stats(demands, result, courses) if result.status in OK_STATUSES else {}
        )
        below_min_list = [
            {"course_code": code, **sec}
            for code, stat in sorted(by_course.items())
            for sec in stat["below_min_sections"]
        ]

        metrics = {
            **base_metrics,
            "status": result.status,
            "unmet_grad_critical": max(
                0, base_metrics["grad_critical_total"] - base_metrics["grad_critical_served"]
            ),
            "sections_below_min_count": len(below_min_list),
            "sections_below_min": below_min_list,
            "staffing_warnings_count": len(result.staffing_warnings),
            "staffing_warnings": list(result.staffing_warnings),
            "solve_seconds": round(result.solve_time_seconds, 2),
            "projected_students": proj_result["students_created"],
            "projected_demands": proj_result["demands_created"],
            "freshmen_projection_rows_scaled": n_scaled,
            "by_course": by_course,
        }

    return {
        "factor": factor,
        "status": result.status,
        "metrics": metrics,
        "scratch_db_path": scratch_path,
    }


def _analyze_fragility(
    variants: list[dict], baseline_factor: float = BASELINE_FACTOR
) -> tuple[list[dict], list[dict]]:
    """Cross-variant, per-course fragility flags relative to the baseline.

    fragile-low: >=1 section below the course's min_enrollment at some
        factor <= 0.9 that is NOT also below min_enrollment at baseline —
        a smaller class kills a section that was fine at the projected size.
    fragile-high: unmet demand (students without seats — this also covers
        "a capacity bound is hit", since a plateaued enrollment against
        growing demand shows up as unmet > 0) at some factor >= 1.1 that is
        NOT also unmet at baseline — a bigger class overflows capacity that
        was fine at the projected size.

    Only successfully solved variants carry by_course data (_course_stats
    is skipped for a failed solve), so a failed variant's factor can never
    spuriously trigger either flag here — its failure is reported
    separately as its own top-line finding.
    """
    by_factor = {v["factor"]: v for v in variants}
    baseline = by_factor.get(baseline_factor)
    if baseline is None:
        return [], []
    baseline_by_course = baseline["metrics"].get("by_course", {})

    all_courses: set[str] = set()
    for v in variants:
        all_courses.update(v["metrics"].get("by_course", {}).keys())

    fragile_low: list[dict] = []
    fragile_high: list[dict] = []
    for code in sorted(all_courses):
        base_stat = baseline_by_course.get(code)
        base_below_min = bool(base_stat and base_stat["below_min"])
        base_unmet = base_stat["unmet"] if base_stat else 0

        low_hits = []
        high_hits = []
        for v in variants:
            factor = v["factor"]
            stat = v["metrics"].get("by_course", {}).get(code)
            if stat is None:
                continue
            if factor <= 0.9 and stat["below_min"] and not base_below_min:
                low_hits.append({
                    "factor": factor,
                    "enrolled": stat["enrolled"],
                    "min_enrollment": stat["min_enrollment"],
                })
            if factor >= 1.1 and stat["unmet"] > 0 and base_unmet == 0:
                high_hits.append({
                    "factor": factor,
                    "enrolled": stat["enrolled"],
                    "demand": stat["demand"],
                    "unmet": stat["unmet"],
                })

        if low_hits:
            fragile_low.append({
                "course_code": code,
                "baseline_enrolled": base_stat["enrolled"] if base_stat else 0,
                "min_enrollment": low_hits[0]["min_enrollment"],
                "triggers": low_hits,
            })
        if high_hits:
            fragile_high.append({
                "course_code": code,
                "baseline_enrolled": base_stat["enrolled"] if base_stat else 0,
                "baseline_demand": base_stat["demand"] if base_stat else 0,
                "triggers": high_hits,
            })
    return fragile_low, fragile_high


def stress_test(
    source_db: Path,
    semester: str,
    factors: list[float] = DEFAULT_FACTORS,
    time_limit: int = 60,
    workers: int = 0,
    log: Callable[[str], None] = print,
    config: SchedulerConfig | None = None,
) -> dict:
    """Solve `semester` once per incoming-freshman-class scaling in `factors`.

    Each factor gets its own throwaway scratch copy of `source_db` (never
    opened for writing) with every ACTIVE FreshmenProjection.estimated_count
    scaled deterministically, projected demand regenerated from the scaled
    counts, and a full solve at the same `time_limit`/`workers` budget as
    every other factor. Real (returning-student) demand is untouched, so it
    is identical across every variant.

    If the source has no active FreshmenProjection rows, this returns
    immediately with `nothing_to_stress=True` rather than solving `factors`
    identical variants — nothing would vary between them.

    Args:
        source_db: Path to the source database (read-only; never written).
        semester: Semester to solve, e.g. "2026FA".
        factors: Incoming-class scaling factors (1.0 = as projected).
        time_limit: Solver seconds PER FACTOR (total wall time is roughly
            len(factors) * time_limit).
        workers: CP-SAT worker threads (0 = auto).
        log: Callable receiving progress strings.
        config: Override SchedulerConfig; defaults to
            SchedulerConfig.load_default() (the repo's config/constraints.yaml
            when present). Tests typically pass a bare, fast SchedulerConfig()
            here instead — see tests/test_scenarios.py's _fast_config for the
            established pattern this mirrors.

    Returns:
        {"semester", "factors", "variants": [{"factor", "status", "metrics",
        "scratch_db_path"}, ...], "fragile_low": [...], "fragile_high": [...],
        "baseline_factor"} — or, when there is nothing to stress, the same
        shape with empty "variants"/"fragile_low"/"fragile_high" plus
        "nothing_to_stress": True and a "message" explaining why.
    """
    source = Path(source_db)
    if not source.exists():
        raise FileNotFoundError(f"Source database not found: {source}")

    factors = list(factors)

    log(f"Checking for active freshmen projections in {source}…")
    if _count_active_projections(source) == 0:
        msg = (
            "No active FreshmenProjection rows found — nothing to stress "
            "(every factor would scale zero rows and solve an identical "
            "schedule). Add a FreshmenProjection row for the incoming class "
            "and re-run."
        )
        log(msg)
        return {
            "semester": semester,
            "factors": factors,
            "variants": [],
            "fragile_low": [],
            "fragile_high": [],
            "baseline_factor": BASELINE_FACTOR,
            "nothing_to_stress": True,
            "message": msg,
        }

    if config is None:
        config = SchedulerConfig.load_default()
    config.solver_settings["max_time_seconds"] = time_limit
    config.solver_settings["num_workers"] = workers

    variants: list[dict] = []
    for i, factor in enumerate(factors):
        log(f"[{i + 1}/{len(factors)}] factor {factor}x: scratch copy…")
        scratch_path = scratch_copy(source, f"stress_{str(factor).replace('.', '_')}")
        log(f"  scratch: {scratch_path}")
        variant = _solve_variant(scratch_path, semester, factor, config, log)
        variants.append(variant)

    fragile_low, fragile_high = _analyze_fragility(variants, BASELINE_FACTOR)
    log(
        f"Done: {len(fragile_low)} fragile-low, {len(fragile_high)} "
        f"fragile-high course(s) flagged."
    )

    return {
        "semester": semester,
        "factors": factors,
        "variants": variants,
        "fragile_low": fragile_low,
        "fragile_high": fragile_high,
        "baseline_factor": BASELINE_FACTOR,
    }


def _factor_label(factor: float) -> str:
    """0.8 -> '0.8x', 1.0 -> '1.0x', 1.25 -> '1.25x'."""
    text = f"{factor:.2f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return f"{text}x"


def _pct(factor: float) -> str:
    """1.2 -> '+20%', 0.8 -> '-20%', 1.0 -> '0%'."""
    delta = round((factor - 1.0) * 100)
    return "0%" if delta == 0 else f"{delta:+d}%"


def _executive_summary(
    variants: list[dict],
    factors: list[float],
    baseline_factor: float,
    fragile_low: list[dict],
    fragile_high: list[dict],
) -> list[str]:
    """Plain-language headline: the safe range around baseline, then one
    call-out line per factor outside it (a failed solve, or a specific
    course flagged fragile at that factor)."""
    by_factor = {v["factor"]: v for v in variants}
    sorted_factors = sorted(factors)

    flagged: dict[float, list[str]] = defaultdict(list)
    for item in fragile_low:
        for t in item["triggers"]:
            flagged[t["factor"]].append(
                f"{item['course_code']} drops below its "
                f"{t['min_enrollment']}-student minimum ({t['enrolled']} enrolled)"
            )
    for item in fragile_high:
        for t in item["triggers"]:
            flagged[t["factor"]].append(
                f"{item['course_code']} overflows by {t['unmet']} seat(s) "
                f"({t['enrolled']}/{t['demand']} seated)"
            )

    base_variant = by_factor.get(baseline_factor)
    if base_variant is None:
        return [
            f"Baseline factor {_factor_label(baseline_factor)} was not "
            f"included in the factors tested, so no fragility comparison "
            f"could be made — read each column below standalone."
        ]
    if base_variant["status"] not in OK_STATUSES:
        lines = [
            f"Baseline factor {_factor_label(baseline_factor)} itself "
            f"returned '{base_variant['status']}' — the schedule does not "
            f"fit the projected class size as-is. Every factor is reported "
            f"standalone below."
        ]
        for f in sorted_factors:
            v = by_factor.get(f)
            if v is not None:
                lines.append(f"- {_factor_label(f)}: {v['status']}.")
        return lines

    def is_safe(f: float) -> bool:
        v = by_factor.get(f)
        return bool(v) and v["status"] in OK_STATUSES and f not in flagged

    base_idx = sorted_factors.index(baseline_factor)
    safe_min = safe_max = baseline_factor
    i = base_idx - 1
    while i >= 0 and is_safe(sorted_factors[i]):
        safe_min = sorted_factors[i]
        i -= 1
    i = base_idx + 1
    while i < len(sorted_factors) and is_safe(sorted_factors[i]):
        safe_max = sorted_factors[i]
        i += 1

    if safe_min == sorted_factors[0] and safe_max == sorted_factors[-1]:
        lines = [
            f"Schedule holds across the full range tested "
            f"({_pct(safe_min)} to {_pct(safe_max)})."
        ]
    else:
        lines = [f"Schedule holds from {_pct(safe_min)} to {_pct(safe_max)}."]

    for f in sorted_factors:
        if safe_min <= f <= safe_max:
            continue
        v = by_factor.get(f)
        if v is None:
            continue
        if v["status"] not in OK_STATUSES:
            lines.append(
                f"- At {_pct(f)}: solver returned '{v['status']}' — this "
                f"class size does not fit the current room/instructor supply."
            )
        for desc in flagged.get(f, []):
            lines.append(f"- At {_pct(f)}: {desc}.")

    return lines


def render_stress_markdown(result: dict) -> str:
    """Render a stress_test() result as a markdown report.

    Deterministic (no timestamps — the caller stamps one on if it wants).
    """
    semester = result["semester"]
    lines = [f"# Enrollment stress test — {semester}", ""]

    if result.get("nothing_to_stress"):
        lines += [
            result.get("message", "Nothing to stress."),
            "",
            "No FreshmenProjection rows are active for this catalog, so "
            "every scaling factor would regenerate identical (zero) "
            "projected demand and solve an identical schedule. Add a "
            "projection row for the incoming class, then re-run.",
            "",
        ]
        return "\n".join(lines)

    variants = result["variants"]
    factors = result["factors"]
    baseline_factor = result.get("baseline_factor", BASELINE_FACTOR)
    fragile_low = result.get("fragile_low", [])
    fragile_high = result.get("fragile_high", [])
    by_factor = {v["factor"]: v for v in variants}

    lines += [
        "Solves the SAME semester once per incoming-freshman-class size "
        "(scaling every active FreshmenProjection.estimated_count by the "
        "factor shown), on throwaway copies of the source database. "
        "Returning-student demand is identical in every column — only the "
        "projected freshman cohort changes.",
        "",
        "## Executive summary",
        "",
    ]
    lines += _executive_summary(variants, factors, baseline_factor, fragile_low, fragile_high)
    lines += [
        "",
        "**Caveat:** this stresses PROJECTED freshmen demand only — "
        "returning-student demand is held constant across every column. "
        "Only courses that receive projected demand (directly, or via a "
        "requirement chain) can vary between columns; everything else is "
        "identical by construction.",
        "",
        "## Side-by-side variants",
        "",
    ]

    header = "| Metric | " + " | ".join(
        f"{_factor_label(f)}{' (baseline)' if f == baseline_factor else ''}" for f in factors
    ) + " |"
    sep = "|---|" + "---:|" * len(factors)
    lines += [header, sep]
    for label, key in METRIC_ROWS:
        cells = [format_metric_value(key, by_factor[f]["metrics"].get(key) if f in by_factor
                                      else None) for f in factors]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Fragile-low: a smaller class undersizes these courses", ""]
    if fragile_low:
        lines += [
            "| Course | Baseline enrolled | Min enrollment | Triggered at |",
            "|---|---:|---:|---|",
        ]
        for item in fragile_low:
            triggers = "; ".join(
                f"{_factor_label(t['factor'])} ({t['enrolled']} enrolled)"
                for t in item["triggers"]
            )
            lines.append(
                f"| {item['course_code']} | {item['baseline_enrolled']} "
                f"| {item['min_enrollment']} | {triggers} |"
            )
    else:
        lines.append("- none detected")
    lines.append("")

    lines += ["## Fragile-high: a bigger class overflows these courses", ""]
    if fragile_high:
        lines += [
            "| Course | Baseline enrolled/demand | Triggered at |",
            "|---|---|---|",
        ]
        for item in fragile_high:
            triggers = "; ".join(
                f"{_factor_label(t['factor'])} (unmet {t['unmet']}, "
                f"{t['enrolled']}/{t['demand']} seated)"
                for t in item["triggers"]
            )
            lines.append(
                f"| {item['course_code']} | {item['baseline_enrolled']}/"
                f"{item['baseline_demand']} | {triggers} |"
            )
    else:
        lines.append("- none detected")
    lines.append("")

    below_min_any = any(v["metrics"].get("sections_below_min") for v in variants)
    if below_min_any:
        lines += ["## Sections below minimum enrollment (all variants)", ""]
        lines += ["| Factor | Course | Section | Enrolled | Minimum |", "|---|---|---|---:|---:|"]
        for v in variants:
            for row in v["metrics"].get("sections_below_min", []):
                lines.append(
                    f"| {_factor_label(v['factor'])} | {row['course_code']} "
                    f"| {row['section_number']} | {row['enrolled']} "
                    f"| {row['min_enrollment']} |"
                )
        lines.append("")

    warnings_any = any(v["metrics"].get("staffing_warnings") for v in variants)
    if warnings_any:
        lines += ["## Staffing warnings by variant", ""]
        for v in variants:
            ws = v["metrics"].get("staffing_warnings", [])
            if ws:
                lines.append(f"**{_factor_label(v['factor'])}:**")
                lines += [f"- {w}" for w in ws]
                lines.append("")

    failed = [v for v in variants if v["status"] not in OK_STATUSES]
    lines += ["## Failed variants", ""]
    if failed:
        for v in failed:
            lines.append(
                f"- **{_factor_label(v['factor'])}**: solver returned "
                f"'{v['status']}' — this class size does not fit the "
                f"current room/instructor supply within the time budget."
            )
    else:
        lines.append("- none — every factor solved (optimal or feasible)")
    lines.append("")

    return "\n".join(lines)
