"""What-if scenario runner: resource tweaks on a throwaway DB copy, re-solved and diffed.

"What if we hire another chemist / add a lab room / lose an instructor?" This
module answers that by solving the SAME semester twice against two throwaway
copies of the source database — one BASELINE (as-is) and one with a set of
resource tweaks (add/remove faculty, add/remove rooms, cap a class size)
applied before solving — using the same solver config and time budget on
both sides, then diffing the resulting metrics. The source database is never
opened for writing; see src/core/longrange.py for the same scratch-copy
pattern (sqlite3 online-backup API, never shutil — WAL sidecars need it).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from src.core.conflicts import build_conflict_matrix
from src.core.demand import DemandCalculator
from src.core.engine import SchedulerConfig, SchedulingEngine, SolverResult
from src.core.graph import PrerequisiteGraph
from src.core.scratchdb import scratch_copy
from src.database.connection import create_tables, get_engine
from src.database.models import (
    Course,
    Faculty,
    Room,
    RoomType,
    StudentDemand,
    TimeSlot,
)


@dataclass
class ScenarioTweaks:
    """Resource changes to apply to the scenario copy before solving.

    Faculty/room dicts mirror their model constructors (unspecified optional
    keys fall back to model defaults); `remove_faculty`/`remove_rooms` are
    lists of ids. Removing an id that doesn't exist in the copy is a no-op.
    """

    add_faculty: list[dict] = field(default_factory=list)
    remove_faculty: list[str] = field(default_factory=list)
    add_rooms: list[dict] = field(default_factory=list)
    remove_rooms: list[str] = field(default_factory=list)
    set_max_class_size: dict[str, int] = field(default_factory=dict)


@dataclass
class ScenarioResult:
    """Baseline-vs-scenario comparison produced by one run_scenario() call."""

    semester: str
    tweaks: ScenarioTweaks
    time_limit_seconds: int = 0
    baseline_status: str = "unknown"
    scenario_status: str = "unknown"
    baseline_metrics: dict = field(default_factory=dict)
    scenario_metrics: dict = field(default_factory=dict)
    baseline_staffing_warnings: list[str] = field(default_factory=list)
    scenario_staffing_warnings: list[str] = field(default_factory=list)
    baseline_solve_seconds: float = 0.0
    scenario_solve_seconds: float = 0.0
    baseline_optimality_gap_pct: float | None = None
    scenario_optimality_gap_pct: float | None = None
    deltas: dict = field(default_factory=dict)
    # Scratch copy paths (kept, not deleted — same call as longrange.py's
    # project_terms keeping its scratch DB: lets a caller inspect the two
    # generated schedules afterward). Not opened for writing again by us.
    baseline_db_path: Path | None = None
    scenario_db_path: Path | None = None


# Rows shown in both the markdown report and the CLI console table: the
# same metric set as scripts/retrospective_sim.py's _COMPARISON_ROWS,
# trimmed to a baseline-vs-scenario comparison (no "actual schedule" side).
METRIC_ROWS: list[tuple[str, str]] = [
    ("Sections scheduled", "sections"),
    ("Total enrollment", "total_enrollment"),
    ("Demands with offered sections", "demands_with_sections"),
    ("Total demands", "demands_total"),
    ("Grad-critical demands", "grad_critical_total"),
    ("Grad-critical offered", "grad_critical_offered"),
    ("Grad-critical enrolled", "grad_critical_served"),
    ("Conflicting section pairs", "conflict_pairs"),
    ("Room-slot pairs used", "room_slots_used"),
    ("Room utilization %", "room_util_pct"),
    ("Distinct rooms used", "rooms_used"),
]


def format_metric_value(key: str, value) -> str:
    """Render one metric cell (percentage-aware)."""
    if value is None:
        return "—"
    if key == "room_util_pct":
        return f"{value:.1f}%"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def format_metric_delta(key: str, value) -> str:
    """Render one delta cell (signed; percentage POINTS for the pct metric)."""
    if value is None:
        return "—"
    if key == "room_util_pct":
        return f"{value:+.1f}pp"
    if isinstance(value, float):
        return f"{value:+.1f}"
    return f"{value:+d}"


def compute_scenario_metrics(
    result: SolverResult,
    demands: list[StudentDemand],
    rooms: list[Room],
    time_slots: list[TimeSlot],
    slot_overlaps: dict[int, set[int]],
) -> dict:
    """The same metric set as scripts/retrospective_sim.compute_gradimize_metrics.

    Lifted rather than imported: src/ must not depend on scripts/. The other
    half of that script, compute_actual_metrics, is retrospective-only (it
    parses a historical schedule CSV that has no equivalent on either side
    of a what-if run) and isn't needed here.
    """
    sections = result.sections or []
    enrollments = result.enrollments or []
    meetings = result.meetings or []

    gradimize_courses = {s.course_code for s in sections}

    grad_critical = [d for d in demands if d.is_graduation_critical]
    grad_offered = sum(1 for d in grad_critical if d.course_code in gradimize_courses)
    section_by_id = {s.id: s for s in sections}
    grad_enrolled = set()
    for e in enrollments:
        sec = section_by_id.get(e.section_id)
        if sec:
            for d in grad_critical:
                if d.student_id == e.student_id and d.course_code == sec.course_code:
                    grad_enrolled.add((d.student_id, d.course_code))
    grad_served = len(grad_enrolled)

    used_room_slots: set[tuple] = set()
    room_counts: dict[str, int] = defaultdict(int)
    for m in meetings:
        used_room_slots.add((m.room_id, m.time_slot_id))
        room_counts[m.room_id] += 1

    total_room_slots = len({r.id for r in rooms}) * len({s.id for s in time_slots})

    demand_by_course: dict[str, int] = defaultdict(int)
    for d in demands:
        demand_by_course[d.course_code] += 1
    demands_with_sections = sum(
        count for cc, count in demand_by_course.items() if cc in gradimize_courses
    )

    demand_students: dict[str, set[str]] = defaultdict(set)
    for d in demands:
        demand_students[d.course_code].add(d.student_id)
    slot_by_section = {
        m.section_id: m.time_slot_id for m in meetings if m.meeting_sequence == 0
    }
    placed = [
        (s.course_code, slot_by_section[s.id])
        for s in sections
        if s.id in slot_by_section
    ]
    conflict_pairs = 0
    for i, (c1, sl1) in enumerate(placed):
        for c2, sl2 in placed[i + 1 :]:
            if c1 == c2:
                continue
            if sl1 == sl2 or sl2 in slot_overlaps.get(sl1, set()):
                if demand_students[c1] & demand_students[c2]:
                    conflict_pairs += 1

    return {
        "sections": len(sections),
        "grad_critical_total": len(grad_critical),
        "grad_critical_served": grad_served,
        "grad_critical_offered": grad_offered,
        "conflict_pairs": conflict_pairs,
        "room_slots_used": len(used_room_slots),
        "room_slots_total": total_room_slots,
        "room_util_pct": (
            (len(used_room_slots) / total_room_slots * 100) if total_room_slots else 0
        ),
        "rooms_used": len(room_counts),
        "total_enrollment": len(enrollments),
        "demands_with_sections": demands_with_sections,
        "demands_total": len(demands),
    }


def _apply_tweaks(session: Session, tweaks: ScenarioTweaks) -> None:
    """Mutate a scratch-copy session in place: removals, then additions,
    then class-size caps.

    Deletes need no cascade: a removed faculty/room id may still be
    referenced by Section/SectionMeeting rows left over from a PRIOR solve
    sitting in the copy, but create_sections()/solve() build a brand new
    ScheduleVersion from current demand on every call — those old rows are
    never read back — and PRAGMA foreign_keys stays off (see
    connection.py), so the delete itself can't fail on a dangling
    reference either.
    """
    for fid in tweaks.remove_faculty:
        fac = session.get(Faculty, fid)
        if fac is not None:
            session.delete(fac)

    for rid in tweaks.remove_rooms:
        room = session.get(Room, rid)
        if room is not None:
            session.delete(room)

    for f in tweaks.add_faculty:
        session.add(
            Faculty(
                id=f["id"],
                name=f.get("name", f["id"]),
                email=f.get("email", ""),
                max_credits=int(f.get("max_credits", 12)),
                approved_courses=list(f.get("approved_courses", [])),
            )
        )

    for r in tweaks.add_rooms:
        room_type = r.get("room_type", RoomType.LECTURE)
        if isinstance(room_type, str):
            room_type = RoomType(room_type)
        session.add(
            Room(
                id=r["id"],
                building=r.get("building", ""),
                number=r.get("number", ""),
                capacity=int(r["capacity"]),
                room_type=room_type,
            )
        )

    for code, cap in tweaks.set_max_class_size.items():
        course = session.get(Course, code)
        if course is not None:
            course.max_class_size = int(cap)
            session.add(course)

    session.commit()


def solve_copy(
    scratch_path: Path,
    semester: str,
    config: SchedulerConfig,
    schedule_name: str,
    tweaks: "ScenarioTweaks | None",
    say: Callable[[str], None],
) -> tuple[SolverResult, dict]:
    """Apply tweaks (if given), ensure demand exists, solve, compute metrics."""
    engine = get_engine(scratch_path)
    create_tables(engine)
    with Session(engine) as session:
        if tweaks is not None:
            _apply_tweaks(session, tweaks)

        demands = list(session.exec(select(StudentDemand)).all())
        if not demands:
            say("  no demand rows found — computing via internal DemandCalculator…")
            graph = PrerequisiteGraph(session)
            DemandCalculator(session, graph).calculate_all_demands()
            demands = list(session.exec(select(StudentDemand)).all())

        conflict_matrix = build_conflict_matrix(session)
        sched_engine = SchedulingEngine(session, conflict_matrix, config)
        sections = sched_engine.create_sections(semester)
        result = sched_engine.solve(sections, schedule_name, semester)

        rooms = list(session.exec(select(Room)).all())
        time_slots = list(session.exec(select(TimeSlot)).all())
        metrics = compute_scenario_metrics(
            result, demands, rooms, time_slots, sched_engine.slot_overlaps
        )
    return result, metrics


def run_scenario(
    source_db_path: Path | str,
    semester: str,
    tweaks: ScenarioTweaks,
    config: SchedulerConfig | None = None,
    time_limit: int = 120,
    emit: "Callable[[str], None] | None" = None,
) -> ScenarioResult:
    """Solve BASELINE and TWEAKED copies of `source_db_path` and diff them.

    Two throwaway scratch copies (sqlite3 online-backup API) are made from
    the same source snapshot: copy A solves as-is, copy B gets `tweaks`
    applied first. Same solver config and time budget on both sides, so any
    metric difference is attributable to the tweaks rather than solver
    variance. The source database is never opened for writing.
    """
    say = emit or (lambda m: None)
    source = Path(source_db_path)
    if not source.exists():
        raise FileNotFoundError(f"Source database not found: {source}")

    if config is None:
        config = SchedulerConfig.load_default()
    config.solver_settings["max_time_seconds"] = time_limit

    out = ScenarioResult(semester=semester, tweaks=tweaks, time_limit_seconds=time_limit)

    out.baseline_db_path = scratch_copy(source, "scenario_baseline")
    say(f"Baseline scratch copy: {out.baseline_db_path}")
    say(f"[1/2] Solving baseline {semester} (as-is)…")
    baseline_result, out.baseline_metrics = solve_copy(
        out.baseline_db_path, semester, config, f"What-If Baseline {semester}", None, say
    )
    out.baseline_status = baseline_result.status
    out.baseline_staffing_warnings = list(baseline_result.staffing_warnings)
    out.baseline_solve_seconds = baseline_result.solve_time_seconds
    out.baseline_optimality_gap_pct = (baseline_result.stats or {}).get("optimality_gap_pct")
    say(f"  baseline: {out.baseline_status}")

    out.scenario_db_path = scratch_copy(source, "scenario")
    say(f"Scenario scratch copy: {out.scenario_db_path}")
    say(f"[2/2] Solving scenario {semester} (tweaks applied)…")
    scenario_result, out.scenario_metrics = solve_copy(
        out.scenario_db_path, semester, config, f"What-If Scenario {semester}", tweaks, say
    )
    out.scenario_status = scenario_result.status
    out.scenario_staffing_warnings = list(scenario_result.staffing_warnings)
    out.scenario_solve_seconds = scenario_result.solve_time_seconds
    out.scenario_optimality_gap_pct = (scenario_result.stats or {}).get("optimality_gap_pct")
    say(f"  scenario: {out.scenario_status}")

    for key, base_val in out.baseline_metrics.items():
        scen_val = out.scenario_metrics.get(key)
        if isinstance(base_val, (int, float)) and isinstance(scen_val, (int, float)):
            out.deltas[key] = scen_val - base_val

    return out


def _tweak_lines(tweaks: ScenarioTweaks) -> list[str]:
    lines = []
    for f in tweaks.add_faculty:
        courses = ", ".join(f.get("approved_courses", [])) or "any course"
        lines.append(
            f"- **Add faculty** `{f['id']}` — {f.get('name', f['id'])}, "
            f"approved for {courses}, max {f.get('max_credits', 12)} credits"
        )
    for fid in tweaks.remove_faculty:
        lines.append(f"- **Remove faculty** `{fid}`")
    for r in tweaks.add_rooms:
        lines.append(
            f"- **Add room** `{r['id']}` — {r.get('building', '?')} "
            f"{r.get('number', '?')}, capacity {r.get('capacity', '?')}, "
            f"type {r.get('room_type', 'LECTURE')}"
        )
    for rid in tweaks.remove_rooms:
        lines.append(f"- **Remove room** `{rid}`")
    for code, cap in tweaks.set_max_class_size.items():
        lines.append(f"- **Cap class size** `{code}` at {cap}")
    return lines


def write_scenario_report(result: ScenarioResult, path: Path | str) -> None:
    """Write the baseline-vs-scenario comparison as a markdown report."""
    lines = [
        f"# What-if scenario — {result.semester}",
        "",
        "Two full CP-SAT solves on throwaway copies of the same source data:",
        "one BASELINE (as-is) and one with the tweaks below applied before",
        "solving. Same solver config and time budget on both sides. The",
        "source database was never written to.",
        "",
        f"- Time budget: {result.time_limit_seconds}s per side",
        f"- Baseline solve: {result.baseline_status} in "
        f"{result.baseline_solve_seconds:.0f}s",
        f"- Scenario solve: {result.scenario_status} in "
        f"{result.scenario_solve_seconds:.0f}s",
        "",
        "## Tweaks applied",
        "",
    ]
    tweak_lines = _tweak_lines(result.tweaks)
    lines += (
        tweak_lines
        if tweak_lines
        else ["- (none — this is a baseline-vs-baseline sanity run)"]
    )

    lines += [
        "",
        "## Side-by-side metrics",
        "",
        "| Metric | Baseline | Scenario | Delta |",
        "|---|---:|---:|---:|",
    ]
    for label, key in METRIC_ROWS:
        b = format_metric_value(key, result.baseline_metrics.get(key))
        s = format_metric_value(key, result.scenario_metrics.get(key))
        d = format_metric_delta(key, result.deltas.get(key))
        lines.append(f"| {label} | {b} | {s} | {d} |")

    lines += ["", "## Staffing warnings", "", "**Baseline:**", ""]
    lines += (
        [f"- {w}" for w in result.baseline_staffing_warnings]
        if result.baseline_staffing_warnings
        else ["- none"]
    )
    lines += ["", "**Scenario:**", ""]
    lines += (
        [f"- {w}" for w in result.scenario_staffing_warnings]
        if result.scenario_staffing_warnings
        else ["- none"]
    )

    caveats: list[str] = []
    for label, status, gap in (
        ("Baseline", result.baseline_status, result.baseline_optimality_gap_pct),
        ("Scenario", result.scenario_status, result.scenario_optimality_gap_pct),
    ):
        if status != "optimal":
            gap_txt = f" (gap {gap:.1f}%)" if gap is not None else ""
            caveats.append(
                f"- {label} solve returned '{status}'{gap_txt}, not proven optimal "
                f"within the {result.time_limit_seconds}s budget — its metrics "
                "are the best schedule FOUND, not necessarily the best POSSIBLE. "
                "Widen --time-limit before trusting a small delta."
            )
    lines += ["", "## Honesty check", ""]
    lines += caveats if caveats else ["- Both sides solved to proven optimality within budget."]
    lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")
