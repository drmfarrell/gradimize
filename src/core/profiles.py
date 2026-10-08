"""Policy profiles: named weight bundles solved side-by-side — a Pareto table.

"Whose schedule is this, anyway?" Every soft-constraint weight is a policy
choice: protecting students from conflicts and 8am classes trades against
compact faculty teaching days trades against running fewer, fuller sections.
This module makes the trade-off visible instead of implicit: solve the SAME
term under named policy profiles (Students First / Faculty First /
Efficiency First / the shipped Balanced defaults) on throwaway DB copies —
same data, same time budget — and put the stakeholder metrics side-by-side
so leadership can pick a point on the curve deliberately.

Builds on the what-if scenario runner's scratch-copy machinery
(src/core/scenarios.py); the source database is never opened for writing.
"""

from collections import defaultdict
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from src.core.engine import SchedulerConfig
from src.core.scenarios import format_metric_value, solve_copy
from src.core.schedule_quality import analyze_student_quality
from src.core.scratchdb import scratch_copy
from src.database.connection import get_engine
from src.database.models import (
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

# Each profile is a set of OVERRIDES on top of the loaded config defaults.
# "soft" entries replace whole soft_constraints subdicts; a missing key
# keeps the default. Weights are policy statements — the docstrings under
# "description" are what leadership reads next to the numbers.
PROFILES: dict[str, dict] = {
    "balanced": {
        "description": (
            "The shipped constraints.yaml defaults — the compromise point "
            "the other profiles deviate from."
        ),
    },
    "students_first": {
        "description": (
            "Minimize student pain: much stricter about course-pair "
            "conflicts (threshold 20), heavier 8am/late penalties, faculty "
            "convenience weights mostly zeroed."
        ),
        "conflict_urgency_threshold": 20.0,
        "soft": {
            "early_morning_penalty": {"hour": 8, "weight": -25},
            "late_afternoon_penalty": {"hour": 17, "weight": -25},
            "faculty_prep_penalty": {"weight": -5},
            "faculty_teaching_day_penalty": {"weight": 0},
            "back_to_back_penalty": {"faculty": {"weight": -5}},
            "faculty_preference_bonus": {"weight": 2},
        },
    },
    "faculty_first": {
        "description": (
            "Protect teaching schedules: few preps, compact teaching days, "
            "no back-to-back marathons, preferences honored; student "
            "time-of-day comfort weights lowered."
        ),
        "soft": {
            "faculty_prep_penalty": {"weight": -40},
            "faculty_teaching_day_penalty": {"weight": -30},
            "back_to_back_penalty": {"faculty": {"weight": -40}},
            "faculty_preference_bonus": {"weight": 20},
            "early_morning_penalty": {"hour": 8, "weight": -3},
            "late_afternoon_penalty": {"hour": 17, "weight": -3},
        },
    },
    "efficiency_first": {
        "description": (
            "Run lean: section closure ON with a strong consolidation push "
            "(fewer, fuller sections), balanced room/day usage, relaxed "
            "conflict threshold (80)."
        ),
        "conflict_urgency_threshold": 80.0,
        "section_settings": {"allow_section_closure": True},
        "soft": {
            "open_section_penalty": {"weight": -40},
            "room_balance_penalty": {"weight": -15},
            "day_balance_penalty": {"weight": -25},
        },
    },
}

# (label, key, stakeholder) rows for the Pareto table, in display order.
PARETO_ROWS: list[tuple[str, str, str]] = [
    ("Total enrollment", "total_enrollment", "students"),
    ("Grad-critical enrolled", "grad_critical_served", "students"),
    ("Conflicting section pairs", "conflict_pairs", "students"),
    ("Mean days on campus", "mean_days_on_campus", "students"),
    ("Mean idle gap (min)", "mean_gap_minutes", "students"),
    ("Students with 5-day weeks", "students_5_day", "students"),
    ("Students with 3h+ gaps", "students_big_gap", "students"),
    ("Mean preps per instructor", "mean_preps", "faculty"),
    ("Mean teaching days", "mean_teaching_days", "faculty"),
    ("Max preps (worst case)", "max_preps", "faculty"),
    ("Sections run", "sections", "university"),
    ("Sections closed by solver", "sections_closed", "university"),
    ("Mean seat fill %", "fill_pct", "university"),
    ("Room utilization %", "room_util_pct", "university"),
    ("Distinct rooms used", "rooms_used", "university"),
]


def apply_profile(config: SchedulerConfig, name: str) -> SchedulerConfig:
    """Apply a named profile's overrides to `config` (mutates and returns it)."""
    if name not in PROFILES:
        raise ValueError(
            f"Unknown profile '{name}' — available: {', '.join(sorted(PROFILES))}"
        )
    profile = PROFILES[name]
    if "conflict_urgency_threshold" in profile:
        config.conflict_urgency_threshold = float(
            profile["conflict_urgency_threshold"]
        )
    for key, sub in profile.get("soft", {}).items():
        config.soft_constraints[key] = sub
    config.section_settings.update(profile.get("section_settings", {}))
    return config


def _faculty_and_fill_metrics(scratch_path: Path, schedule_id: int | None) -> dict:
    """Stakeholder metrics read back from the stored schedule."""
    out = {
        "mean_preps": None, "mean_teaching_days": None, "max_preps": None,
        "fill_pct": None, "mean_days_on_campus": None, "mean_gap_minutes": None,
        "students_5_day": None, "students_big_gap": None,
    }
    if schedule_id is None:
        return out
    engine = get_engine(scratch_path)
    with Session(engine) as session:
        sections = list(session.exec(
            select(Section).where(Section.schedule_id == schedule_id)
        ).all())
        if not sections:
            return out
        slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
        meetings_by_sid = defaultdict(list)
        for m in session.exec(
            select(SectionMeeting).where(
                SectionMeeting.section_id.in_(  # type: ignore[union-attr]
                    [s.id for s in sections if s.id is not None]
                )
            )
        ).all():
            meetings_by_sid[m.section_id].append(m)

        preps: dict[str, set[str]] = defaultdict(set)
        days: dict[str, set[str]] = defaultdict(set)
        for s in sections:
            if not s.faculty_id:
                continue
            preps[s.faculty_id].add(s.course_code)
            for m in meetings_by_sid.get(s.id, []):
                slot = slots.get(m.time_slot_id)
                if slot:
                    days[s.faculty_id].update(slot.days)
        if preps:
            out["mean_preps"] = sum(len(v) for v in preps.values()) / len(preps)
            out["max_preps"] = max(len(v) for v in preps.values())
        if days:
            out["mean_teaching_days"] = (
                sum(len(v) for v in days.values()) / len(days)
            )

        total_capacity = sum(s.capacity for s in sections)
        n_enrolled = len(list(session.exec(
            select(SectionEnrollment).where(
                SectionEnrollment.section_id.in_(  # type: ignore[union-attr]
                    [s.id for s in sections if s.id is not None]
                )
            )
        ).all()))
        if total_capacity:
            out["fill_pct"] = n_enrolled / total_capacity * 100

        quality = analyze_student_quality(session, schedule_id)
        out["mean_days_on_campus"] = quality.mean_days_on_campus
        out["mean_gap_minutes"] = quality.mean_gap_minutes
        out["students_5_day"] = quality.students_5_day
        out["students_big_gap"] = quality.students_big_gap
    return out


def compare_profiles(
    source_db_path: Path | str,
    semester: str,
    profile_names: list[str] | None = None,
    time_limit: int = 120,
    emit: "Callable[[str], None] | None" = None,
) -> dict:
    """Solve the same term once per policy profile; return the Pareto data.

    One scratch copy per profile (sqlite3 online-backup; the source DB is
    never written). Same data and time budget everywhere, so metric
    differences are attributable to the WEIGHTS — i.e. to policy.
    """
    say = emit or (lambda m: None)
    source = Path(source_db_path)
    if not source.exists():
        raise FileNotFoundError(f"Source database not found: {source}")
    names = profile_names or list(PROFILES)
    for n in names:
        if n not in PROFILES:
            raise ValueError(
                f"Unknown profile '{n}' — available: {', '.join(sorted(PROFILES))}"
            )

    rows = []
    for i, name in enumerate(names, 1):
        config = apply_profile(SchedulerConfig.load_default(), name)
        config.solver_settings["max_time_seconds"] = time_limit
        scratch = scratch_copy(source, f"profile_{name}")
        say(f"[{i}/{len(names)}] Solving under '{name}'…")
        result, metrics = solve_copy(
            scratch, semester, config, f"Profile: {name} {semester}", None, say
        )
        schedule_id = (
            result.sections[0].schedule_id if result.sections else None
        )
        metrics["sections_closed"] = (result.stats or {}).get("sections_closed", 0)
        metrics.update(_faculty_and_fill_metrics(scratch, schedule_id))
        say(f"  {name}: {result.status}")
        rows.append({
            "name": name,
            "description": PROFILES[name]["description"],
            "status": result.status,
            "solve_seconds": result.solve_time_seconds,
            "optimality_gap_pct": (result.stats or {}).get("optimality_gap_pct"),
            "staffing_warnings": list(result.staffing_warnings),
            "metrics": metrics,
            "db_path": str(scratch),
        })

    return {
        "semester": semester,
        "time_limit_seconds": time_limit,
        "profiles": rows,
    }


def render_profiles_markdown(result: dict) -> str:
    """The Pareto trade-off table plus per-profile descriptions and caveats."""
    profs = result["profiles"]
    lines = [
        f"# Policy profiles — {result['semester']}",
        "",
        "The same term, the same data, the same time budget "
        f"({result['time_limit_seconds']}s) — solved once per POLICY. "
        "Differences below are what each policy buys, and from whom.",
        "",
        "## Profiles",
        "",
    ]
    for p in profs:
        lines.append(f"- **{p['name']}** ({p['status']}): {p['description']}")
    lines += [
        "",
        "## Trade-off table",
        "",
        "| Metric | " + " | ".join(p["name"] for p in profs) + " |",
        "|---" * (len(profs) + 1) + "|",
    ]
    section_for = {"students": "Students", "faculty": "Faculty",
                   "university": "University"}
    last_group = None
    for label, key, group in PARETO_ROWS:
        if group != last_group:
            lines.append(
                f"| **{section_for[group]}** |" + " |" * len(profs)
            )
            last_group = group
        cells = " | ".join(
            format_metric_value(key, p["metrics"].get(key)) for p in profs
        )
        lines.append(f"| {label} | {cells} |")

    caveats = [
        f"- '{p['name']}' returned {p['status']}"
        + (
            f" (gap {p['optimality_gap_pct']:.1f}%)"
            if p["optimality_gap_pct"] is not None and p["status"] != "optimal"
            else ""
        )
        + " — not proven optimal; treat small deltas with care."
        for p in profs
        if p["status"] != "optimal"
    ]
    lines += ["", "## Honesty check", ""]
    lines += caveats or [
        "- All profiles solved to proven optimality within budget."
    ]
    lines += [
        "",
        "_A policy profile changes WEIGHTS, not facts: rooms, instructors,",
        "and demand are identical in every column. Pick the column whose",
        "trade-offs you can defend, then generate the real schedule with",
        "those weights._",
        "",
    ]
    return "\n".join(lines)
