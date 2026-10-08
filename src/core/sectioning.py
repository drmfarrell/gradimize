"""Exact student sectioning (phase B): seat students on a FIXED timetable.

The joint CP-SAT model (``SchedulingEngine.solve``) decides placement,
staffing and a first cut of enrollment together, but it deliberately has no
per-student time-overlap constraint — at scale those clauses explode — so a
student could be seated in two sections that meet at the same time; course
pairs were only kept apart by soft penalties above
``conflict_urgency_threshold``. Once the timetable is fixed, though,
sectioning is small and separable: each student has a handful of candidate
sections and whether two of them collide is a known constant.

This pass re-decides WHICH students sit in WHICH sections with

- hard: at most one section per (student, course);
- hard: a student never holds two seats that meet at the same time
  (time-point cliques, :mod:`src.core.slotcliques`);
- hard: section capacity, and joint capacity for cross-listed packs;
- hard, falling back to soft with a loud report: graduation-critical
  demands get a seat;
- objective: urgency-weighted seats on the joint model's scale
  (urgency x 100), plus optional repair-mode seat anchors.

Pipeline (each stage only ever improves on the last, and every stage
yields a complete collision-free seating, so the pass never fails):

1. graduation-critical demands are seated EXACTLY by a tiny CP-SAT model
   (a few hundred variables) — if even that is infeasible the unmet ones
   are reported and everything else continues;
2. two deterministic greedy seatings (repairing the caller's hint, and
   from scratch by urgency) around those fixed seats;
3. the LP relaxation (GLOP, ~15 s on 77k variables) — its bound is within
   a fraction of a percent of the integer optimum on this structure, and
   rounding its integral part through the greedy lands within ~1%;
4. CP-SAT polishes from the best seating so far for
   ``time_limit_seconds``; measured on the sample instance it reaches
   ~0.2% of optimal given 600 s (HiGHS proved the optimum in 455 s).

The LP bound makes the reported ``gap_pct`` an honest distance from the
true optimum, not a solver-internal estimate.

It is a pure function over plain inputs so it is unit-testable without a
database; ``SchedulingEngine._extract_solution`` adapts the solved
timetable into it. The pacing-aware seat rebalancer (``seatswap``) runs
after this and only makes collision-free moves, so the guarantee survives.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from src.core.slotcliques import cliques_by_slot

logger = logging.getLogger(__name__)

#: Integer scaling of urgency scores — identical to the joint objective.
URGENCY_SCALE = 100
#: Penalty per unmet graduation-critical demand when the hard form proves
#: infeasible on this timetable. Dwarfs any sum of seat urgencies so the
#: solver never trades a graduating senior's seat for ordinary reseating.
CRITICAL_MISS_WEIGHT = 10**7


@dataclass(frozen=True)
class SectionSpec:
    """A scheduled section as phase B sees it."""

    crn: str
    course_code: str
    #: Effective seats: section capacity, already clamped by the assigned
    #: room(s) when room capacity is respected.
    capacity: int
    #: Time slots of every meeting (empty = unplaced, collides with nothing).
    slot_ids: tuple[int, ...] = ()
    #: Cross-list pack key; members of a pack share one capacity pool.
    pack: str | None = None


@dataclass
class SectioningResult:
    status: str = "unknown"
    #: (student_id, crn) pairs to enroll.
    seats: set[tuple[str, str]] = field(default_factory=set)
    objective: float = 0.0
    #: (student_id, course_code) graduation-critical demands left unseated.
    unmet_graduation_critical: list[tuple[str, str]] = field(default_factory=list)
    #: True when the hard graduation-critical form was infeasible and the
    #: soft fallback produced this result.
    graduation_critical_relaxed: bool = False
    solve_time_seconds: float = 0.0
    n_candidates: int = 0
    n_overlap_constraints: int = 0
    best_bound: float = 0.0
    gap_pct: float | None = None
    #: Which stage produced the final seating: "greedy", "lp-round" or "cp-sat".
    method: str = "greedy"
    #: Objective of the best greedy seating (stage 2).
    greedy_objective: float = 0.0
    #: Objective of the best seating before the CP-SAT polish (stages 1-3).
    warm_objective: float = 0.0
    #: Upper bound from the LP relaxation (None if the LP stage did not run).
    lp_bound: float | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("optimal", "feasible")


def count_student_overlaps(
    seats: Iterable[tuple[str, str]],
    sections: Iterable[SectionSpec],
    slot_cliques: list[frozenset[int]],
) -> int:
    """Number of distinct (student, section-pair) time collisions in ``seats``.

    This is the metric phase B drives to zero; the engine reports how many
    the joint model's own enrollment carried.
    """
    by_crn = {s.crn: s for s in sections}
    clique_index = cliques_by_slot(slot_cliques)
    cliques_of_crn = {
        crn: {c for slot_id in s.slot_ids for c in clique_index.get(slot_id, ())}
        for crn, s in by_crn.items()
    }
    by_student: dict[str, list[str]] = defaultdict(list)
    for student_id, crn in seats:
        if crn in by_crn:
            by_student[student_id].append(crn)
    collisions = 0
    for crns in by_student.values():
        seen: set[tuple[str, str]] = set()
        by_clique: dict[int, list[str]] = defaultdict(list)
        for crn in crns:
            for c in cliques_of_crn[crn]:
                by_clique[c].append(crn)
        for members in by_clique.values():
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    seen.add((min(a, b), max(a, b)))
        collisions += len(seen)
    return collisions


def greedy_seats(
    sections: Iterable[SectionSpec],
    demands: Iterable[Any],
    slot_cliques: list[frozenset[int]],
    *,
    start: Iterable[tuple[str, str]] | None = None,
    seat_anchor: dict[tuple[str, str], int] | None = None,
    fixed: Iterable[tuple[str, str]] | None = None,
) -> set[tuple[str, str]]:
    """Deterministic collision-free seating: fix, repair ``start``, then fill.

    ``fixed`` seats (e.g. the exact graduation-critical seating) are taken
    first, in order, whenever they fit. Then every seat in ``start``
    (typically the joint model's Z or a rounded LP solution) is kept if it
    does not collide with a seat already kept for that student, taking
    graduation-critical and higher-urgency seats first. Finally the
    still-unseated demands are walked in the same priority order and each
    is seated in the first section of its course with a free seat that fits
    the student's week. The result respects capacity, pack capacity,
    one-section-per-course and no-overlap by construction, so it is always
    a valid complete warm start for :func:`section_students` — and a sound
    fallback if the solver finds nothing better in its budget.
    """
    sections = list(sections)
    by_crn = {s.crn: s for s in sections}
    by_course: dict[str, list[SectionSpec]] = defaultdict(list)
    for s in sections:
        by_course[s.course_code].append(s)
    clique_index = cliques_by_slot(slot_cliques)
    cliques_of_crn = {
        crn: frozenset(c for slot_id in s.slot_ids for c in clique_index.get(slot_id, ()))
        for crn, s in by_crn.items()
    }
    anchors = seat_anchor or {}

    wanted: dict[tuple[str, str], tuple[float, bool]] = {}
    for d in demands:
        key = (d.student_id, d.course_code)
        urgency, critical = wanted.get(key, (0.0, False))
        wanted[key] = (
            max(urgency, float(d.urgency_score)),
            critical or bool(getattr(d, "is_graduation_critical", False)),
        )

    def priority(student_id: str, code: str, crn: str | None = None) -> tuple:
        urgency, critical = wanted.get((student_id, code), (0.0, False))
        bonus = anchors.get((student_id, crn), 0) if crn else 0
        return (not critical, -(urgency * URGENCY_SCALE + bonus), student_id, code, crn or "")

    remaining = {s.crn: max(0, s.capacity) for s in sections}
    pack_remaining: dict[str, int] = {}
    for s in sections:
        if s.pack:
            pack_remaining[s.pack] = min(
                pack_remaining.get(s.pack, max(0, s.capacity)), max(0, s.capacity)
            )
    busy: dict[str, set[int]] = defaultdict(set)      # student -> cliques occupied
    seated_courses: dict[str, set[str]] = defaultdict(set)
    seats: set[tuple[str, str]] = set()

    def can_seat(student_id: str, s: SectionSpec) -> bool:
        if remaining[s.crn] <= 0 or s.course_code in seated_courses[student_id]:
            return False
        if s.pack and pack_remaining.get(s.pack, 0) <= 0:
            return False
        return not (cliques_of_crn[s.crn] & busy[student_id])

    def take(student_id: str, s: SectionSpec) -> None:
        seats.add((student_id, s.crn))
        remaining[s.crn] -= 1
        if s.pack:
            pack_remaining[s.pack] -= 1
        busy[student_id] |= cliques_of_crn[s.crn]
        seated_courses[student_id].add(s.course_code)

    for student_id, crn in fixed or ():
        if crn in by_crn and (student_id, by_crn[crn].course_code) in wanted:
            s = by_crn[crn]
            if can_seat(student_id, s):
                take(student_id, s)

    kept_start = [
        (sid, crn) for sid, crn in (start or ())
        if crn in by_crn and (sid, by_crn[crn].course_code) in wanted
    ]
    for student_id, crn in sorted(
        kept_start, key=lambda k: priority(k[0], by_crn[k[1]].course_code, k[1])
    ):
        s = by_crn[crn]
        if can_seat(student_id, s):
            take(student_id, s)

    for (student_id, code) in sorted(wanted, key=lambda k: priority(*k)):
        if code in seated_courses[student_id]:
            continue
        candidates = sorted(
            by_course.get(code, ()),
            key=lambda s: (-anchors.get((student_id, s.crn), 0), -remaining[s.crn], s.crn),
        )
        for s in candidates:
            if can_seat(student_id, s):
                take(student_id, s)
                break
    return seats


def _seat_critical_exact(
    sections: list[SectionSpec],
    wanted: dict[tuple[str, str], tuple[float, bool]],
    cliques_of_crn: dict[str, list[int]],
    anchors: dict[tuple[str, str], int],
    *,
    num_workers: int,
    time_limit_seconds: float = 30.0,
) -> tuple[set[tuple[str, str]], list[tuple[str, str]]]:
    """Seat every graduation-critical demand exactly (tiny CP-SAT model).

    Only critical demands are variables, so this is a few hundred Booleans
    and solves in well under a second. Returns ``(seats, unmet)``; ``unmet``
    is non-empty only when no seating of the critical demands alone fits
    this timetable, which is then a genuine property of the schedule and
    is reported rather than hidden.
    """
    by_course: dict[str, list[SectionSpec]] = defaultdict(list)
    for s in sections:
        by_course[s.course_code].append(s)
    critical = sorted(k for k, (_u, c) in wanted.items() if c and by_course.get(k[1]))
    if not critical:
        return set(), []

    def build(relax: bool):
        model = cp_model.CpModel()
        Z: dict[tuple[str, str], Any] = {}
        miss: dict[tuple[str, str], Any] = {}
        objective: list[Any] = []
        per_student: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        per_section: dict[str, list[Any]] = defaultdict(list)
        for student_id, code in critical:
            urgency = wanted[student_id, code][0]
            seat_vars = []
            for s in by_course[code]:
                var = model.NewBoolVar(f"c_{student_id}_{s.crn}")
                Z[student_id, s.crn] = var
                seat_vars.append(var)
                per_student[student_id].append((s.crn, var))
                per_section[s.crn].append(var)
                weight = int(round(urgency * URGENCY_SCALE))
                weight += int(anchors.get((student_id, s.crn), 0))
                objective.append(weight * var)
            model.Add(sum(seat_vars) <= 1)
            if relax:
                m = model.NewBoolVar(f"miss_{student_id}_{code}")
                model.Add(sum(seat_vars) + m >= 1)
                miss[student_id, code] = m
                objective.append(-CRITICAL_MISS_WEIGHT * m)
            else:
                model.Add(sum(seat_vars) >= 1)
        for s in sections:
            seat_vars = per_section.get(s.crn)
            if seat_vars:
                model.Add(sum(seat_vars) <= max(0, s.capacity))
        packs: dict[str, list[SectionSpec]] = defaultdict(list)
        for s in sections:
            if s.pack:
                packs[s.pack].append(s)
        for members in packs.values():
            seat_vars = [v for s in members for v in per_section.get(s.crn, ())]
            if len(members) > 1 and seat_vars:
                model.Add(sum(seat_vars) <= max(0, min(s.capacity for s in members)))
        for candidates in per_student.values():
            by_clique: dict[int, list[Any]] = defaultdict(list)
            for crn, var in candidates:
                for c in cliques_of_crn[crn]:
                    by_clique[c].append(var)
            for seat_vars in by_clique.values():
                if len(seat_vars) > 1:
                    model.Add(sum(seat_vars) <= 1)
        model.Maximize(sum(objective))
        return model, Z, miss

    def run(model):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(time_limit_seconds)
        solver.parameters.num_workers = max(1, int(num_workers))
        solver.parameters.log_search_progress = False
        return solver, solver.Solve(model)

    model, Z, miss = build(relax=False)
    solver, status = run(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        model, Z, miss = build(relax=True)
        solver, status = run(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            logger.warning("Exact graduation-critical seating found nothing; continuing greedy")
            return set(), []
    seats = {key for key, var in Z.items() if solver.Value(var)}
    unmet = sorted(key for key, m in miss.items() if solver.Value(m))
    return seats, unmet


def lp_relaxation(
    sections: list[SectionSpec],
    wanted: dict[tuple[str, str], tuple[float, bool]],
    cliques_of_crn: dict[str, list[int]],
    anchors: dict[tuple[str, str], int],
    *,
    time_limit_seconds: float = 120.0,
) -> tuple[float | None, set[tuple[str, str]]]:
    """Solve the LP relaxation with GLOP; return ``(bound, integral_ones)``.

    The relaxation of this packing structure is nearly tight (on the sample
    instance its bound sits 0.016% above the integer optimum) and a simplex
    vertex is ~80% integral, so fixing the integral ones and greedy-filling
    the rest rounds to within ~1%. Returns ``(None, set())`` when MathOpt
    is unavailable or the LP does not finish in its budget.
    """
    try:
        import datetime

        from ortools.math_opt.python import mathopt
    except Exception as exc:  # pragma: no cover - depends on the ortools build
        logger.info("LP relaxation skipped (MathOpt unavailable: %s)", exc)
        return None, set()

    by_course: dict[str, list[SectionSpec]] = defaultdict(list)
    for s in sections:
        by_course[s.course_code].append(s)
    model = mathopt.Model(name="sectioning_lp")
    Z: dict[tuple[str, str], Any] = {}
    per_student: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    per_section: dict[str, list[Any]] = defaultdict(list)
    objective: list[Any] = []
    for (student_id, code), (urgency, critical) in sorted(wanted.items()):
        candidates = by_course.get(code)
        if not candidates:
            continue
        seat_vars = []
        for s in candidates:
            var = model.add_variable(lb=0.0, ub=1.0, name=f"z_{student_id}_{s.crn}")
            Z[student_id, s.crn] = var
            seat_vars.append(var)
            per_student[student_id].append((s.crn, var))
            per_section[s.crn].append(var)
            weight = int(round(urgency * URGENCY_SCALE))
            weight += int(anchors.get((student_id, s.crn), 0))
            objective.append(weight * var)
        if len(seat_vars) > 1:
            model.add_linear_constraint(mathopt.fast_sum(seat_vars) <= 1)
        if critical:
            model.add_linear_constraint(mathopt.fast_sum(seat_vars) >= 1)
    for s in sections:
        seat_vars = per_section.get(s.crn)
        if seat_vars:
            model.add_linear_constraint(mathopt.fast_sum(seat_vars) <= max(0, s.capacity))
    packs: dict[str, list[SectionSpec]] = defaultdict(list)
    for s in sections:
        if s.pack:
            packs[s.pack].append(s)
    for members in packs.values():
        seat_vars = [v for s in members for v in per_section.get(s.crn, ())]
        if len(members) > 1 and seat_vars:
            model.add_linear_constraint(
                mathopt.fast_sum(seat_vars) <= max(0, min(s.capacity for s in members))
            )
    for candidates in per_student.values():
        by_clique: dict[int, list[Any]] = defaultdict(list)
        for crn, var in candidates:
            for c in cliques_of_crn[crn]:
                by_clique[c].append(var)
        for seat_vars in by_clique.values():
            if len(seat_vars) > 1:
                model.add_linear_constraint(mathopt.fast_sum(seat_vars) <= 1)
    if not objective:
        return None, set()
    model.maximize(mathopt.fast_sum(objective))
    try:
        result = mathopt.solve(
            model, mathopt.SolverType.GLOP,
            params=mathopt.SolveParameters(
                time_limit=datetime.timedelta(seconds=float(time_limit_seconds))
            ),
        )
    except Exception as exc:  # pragma: no cover - solver-level failure
        logger.warning("LP relaxation failed (%s); continuing without it", exc)
        return None, set()
    if result.termination.reason != mathopt.TerminationReason.OPTIMAL:
        logger.info(
            "LP relaxation did not finish (%s); continuing without it",
            result.termination.reason.name,
        )
        return None, set()
    values = result.variable_values()
    ones = {key for key, var in Z.items() if values[var] > 1 - 1e-6}
    return float(result.objective_value()), ones


def section_students(
    sections: Iterable[SectionSpec],
    demands: Iterable[Any],
    slot_cliques: list[frozenset[int]],
    *,
    hint: Iterable[tuple[str, str]] | None = None,
    seat_anchor: dict[tuple[str, str], int] | None = None,
    time_limit_seconds: float = 20.0,
    num_workers: int = 8,
    graduation_critical: str = "hard",
    relative_gap_limit: float | None = 0.005,
    linearization_level: int | None = None,
    use_lp: bool = True,
) -> SectioningResult:
    """Seat ``demands`` into ``sections`` without time collisions.

    Method: two deterministic greedy seatings (:func:`greedy_seats` from the
    caller's hint, and from scratch) are built first and the better one is
    kept. On demand-heavy instances that alone lands within a few percent
    of the capacity-only upper bound in well under a second, while CP-SAT's
    own bound on this packing structure is loose — so CP-SAT is used only
    to POLISH, with the greedy as a complete warm start and a short budget
    (``time_limit_seconds``; 0 skips it). The greedy seating is also the
    fallback whenever the solver returns nothing better.

    Args:
        sections: the fixed timetable's open sections.
        demands: objects with ``student_id``, ``course_code``,
            ``urgency_score`` and ``is_graduation_critical`` (StudentDemand
            rows or anything duck-typed alike). Duplicates per
            (student, course) are merged.
        slot_cliques: from :func:`src.core.slotcliques.compute_slot_cliques`.
        hint: seats to warm-start from (typically the joint model's Z).
        time_limit_seconds: CP-SAT polish budget; <= 0 returns the greedy.
        seat_anchor: extra weight per (student, crn) for keeping a published
            seat (repair mode).
        graduation_critical: ``"hard"`` (hard, then soft fallback if that is
            infeasible), ``"soft"``, or ``"off"``.
        relative_gap_limit: stop once the solution is proven within this
            fraction of the bound (0.005 = 0.5%); None = run to the time limit.
        linearization_level: CP-SAT ``linearization_level`` override.
        use_lp: run the LP-relaxation rounding stage (needs MathOpt/GLOP).
    """
    sections = list(sections)
    by_course: dict[str, list[SectionSpec]] = defaultdict(list)
    for s in sections:
        by_course[s.course_code].append(s)

    wanted: dict[tuple[str, str], tuple[float, bool]] = {}
    for d in demands:
        key = (d.student_id, d.course_code)
        urgency, critical = wanted.get(key, (0.0, False))
        wanted[key] = (
            max(urgency, float(d.urgency_score)),
            critical or bool(getattr(d, "is_graduation_critical", False)),
        )

    clique_index = cliques_by_slot(slot_cliques)
    cliques_of_crn = {
        s.crn: sorted({c for slot_id in s.slot_ids for c in clique_index.get(slot_id, ())})
        for s in sections
    }
    anchors = seat_anchor or {}
    hint_keys = set(hint or ())

    result = SectioningResult()
    if not wanted or not sections:
        result.status = "optimal"
        return result

    def build(relax_critical: bool):
        model = cp_model.CpModel()
        Z: dict[tuple[str, str], Any] = {}
        miss: dict[tuple[str, str], Any] = {}
        objective: list[Any] = []
        per_student: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        per_section: dict[str, list[Any]] = defaultdict(list)
        has_critical = False

        for (student_id, code), (urgency, critical) in sorted(wanted.items()):
            candidates = by_course.get(code)
            if not candidates:
                continue
            seat_vars = []
            for s in candidates:
                var = model.NewBoolVar(f"z_{student_id}_{s.crn}")
                Z[student_id, s.crn] = var
                seat_vars.append(var)
                per_student[student_id].append((s.crn, var))
                per_section[s.crn].append(var)
                weight = int(round(urgency * URGENCY_SCALE))
                weight += int(anchors.get((student_id, s.crn), 0))
                if weight:
                    objective.append(weight * var)
            if len(seat_vars) > 1:
                model.Add(sum(seat_vars) <= 1)
            if critical and graduation_critical != "off":
                has_critical = True
                if graduation_critical == "hard" and not relax_critical:
                    model.Add(sum(seat_vars) >= 1)
                else:
                    m = model.NewBoolVar(f"miss_{student_id}_{code}")
                    model.Add(sum(seat_vars) + m >= 1)
                    miss[student_id, code] = m
                    objective.append(-CRITICAL_MISS_WEIGHT * m)

        for s in sections:
            seat_vars = per_section.get(s.crn)
            if seat_vars:
                model.Add(sum(seat_vars) <= max(0, s.capacity))
        packs: dict[str, list[SectionSpec]] = defaultdict(list)
        for s in sections:
            if s.pack:
                packs[s.pack].append(s)
        for members in packs.values():
            if len(members) < 2:
                continue
            seat_vars = [v for s in members for v in per_section.get(s.crn, ())]
            if seat_vars:
                model.Add(sum(seat_vars) <= max(0, min(s.capacity for s in members)))

        n_overlap = 0
        for candidates in per_student.values():
            by_clique: dict[int, list[Any]] = defaultdict(list)
            for crn, var in candidates:
                for c in cliques_of_crn[crn]:
                    by_clique[c].append(var)
            for seat_vars in by_clique.values():
                if len(seat_vars) > 1:
                    model.Add(sum(seat_vars) <= 1)
                    n_overlap += 1

        for key in hint_keys:
            if key in Z:
                model.AddHint(Z[key], 1)
        if objective:
            model.Maximize(sum(objective))
        return model, Z, miss, n_overlap, has_critical

    def run(model):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(time_limit_seconds)
        solver.parameters.num_workers = max(1, int(num_workers))
        solver.parameters.log_search_progress = False
        if relative_gap_limit is not None:
            solver.parameters.relative_gap_limit = float(relative_gap_limit)
        if linearization_level is not None:
            solver.parameters.linearization_level = int(linearization_level)
        return solver, solver.Solve(model)

    t0 = time.monotonic()
    course_of = {s.crn: s.course_code for s in sections}
    critical_keys = {
        key for key, (_u, critical) in wanted.items()
        if critical and graduation_critical != "off" and by_course.get(key[1])
    }

    def seat_weight(student_id: str, crn: str) -> int:
        urgency = wanted[student_id, course_of[crn]][0]
        return int(round(urgency * URGENCY_SCALE)) + int(anchors.get((student_id, crn), 0))

    def unmet_critical(seats: set[tuple[str, str]]) -> list[tuple[str, str]]:
        seated = {(sid, course_of[crn]) for sid, crn in seats}
        return sorted(critical_keys - seated)

    def score(seats: set[tuple[str, str]]) -> float:
        total = sum(seat_weight(sid, crn) for sid, crn in seats)
        return total - CRITICAL_MISS_WEIGHT * len(unmet_critical(seats))

    # LP relaxation first: its bound is the honest yardstick, and its
    # integral part guides every later stage.
    lp_ones: set[tuple[str, str]] = set()
    if use_lp:
        lp_bound, lp_ones = lp_relaxation(sections, wanted, cliques_of_crn, anchors)
        if lp_bound is not None:
            result.lp_bound = lp_bound
    # Graduation-critical seats, exactly — preferring the LP's choices (a
    # +1 tie-break on the urgency x 100 scale) so the fixed seats sit where
    # the global relaxation wanted them.
    critical_seats: set[tuple[str, str]] = set()
    critical_unmet: list[tuple[str, str]] = []
    if critical_keys:
        tie_break = dict(anchors)
        for key in lp_ones:
            if (key[0], course_of[key[1]]) in critical_keys:
                tie_break[key] = tie_break.get(key, 0) + 1
        critical_seats, critical_unmet = _seat_critical_exact(
            sections, wanted, cliques_of_crn, tie_break, num_workers=num_workers,
        )
    # Greedies around the fixed critical seats: from the caller's hint,
    # from scratch, and from the LP's integral part.
    greedy_kw = dict(seat_anchor=anchors, fixed=sorted(critical_seats))
    greedy = max(
        (
            greedy_seats(sections, demands, slot_cliques, start=hint_keys, **greedy_kw),
            greedy_seats(sections, demands, slot_cliques, **greedy_kw),
        ),
        key=score,
    )
    result.greedy_objective = score(greedy)
    best, best_method = greedy, "greedy"
    if lp_ones:
        rounded = greedy_seats(sections, demands, slot_cliques, start=lp_ones, **greedy_kw)
        if score(rounded) > score(best):
            best, best_method = rounded, "lp-round"
    result.warm_objective = score(best)

    def adopt(seats: set[tuple[str, str]], method: str) -> SectioningResult:
        result.method = method
        result.status = "feasible"
        result.seats = set(seats)
        result.objective = score(seats)
        result.unmet_graduation_critical = unmet_critical(seats)
        result.graduation_critical_relaxed = bool(result.unmet_graduation_critical)
        result.solve_time_seconds = time.monotonic() - t0
        if result.lp_bound is not None:
            result.gap_pct = round(
                max(0.0, result.lp_bound - result.objective)
                / max(1.0, abs(result.objective)) * 100.0, 3,
            )
        return result

    if time_limit_seconds <= 0:
        adopt(best, best_method)
        logger.info(
            "Exact sectioning (no polish) via %s: %d seats, %d unmet graduation-critical, "
            "gap %s%%, %.2fs", result.method, len(result.seats),
            len(result.unmet_graduation_critical), result.gap_pct, result.solve_time_seconds,
        )
        return result

    # Stage 4: CP-SAT polish from the best seating so far. A warm start that
    # leaves a critical demand unseated cannot satisfy the hard form, so go
    # straight to the relaxed form in that case.
    hint_keys = set(best)
    relax_first = bool(critical_unmet) and graduation_critical == "hard"
    model, Z, miss, n_overlap, has_critical = build(relax_critical=relax_first)
    solver, status = run(model)
    if relax_first:
        result.graduation_critical_relaxed = True
    if status == cp_model.INFEASIBLE and graduation_critical == "hard" and has_critical:
        logger.warning(
            "Exact sectioning: not every graduation-critical demand can be "
            "seated on this timetable — relaxing them to a heavy penalty and "
            "reporting the unmet ones"
        )
        model, Z, miss, n_overlap, has_critical = build(relax_critical=True)
        solver, status = run(model)
        result.graduation_critical_relaxed = True
    result.n_candidates = len(Z)
    result.n_overlap_constraints = n_overlap
    result.status = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
    }.get(status, "unknown")
    if result.ok:
        result.method = "cp-sat"
        result.objective = solver.ObjectiveValue() if model.HasObjective() else 0.0
        if model.HasObjective():
            result.best_bound = solver.BestObjectiveBound()
            result.gap_pct = round(
                abs(result.best_bound - result.objective)
                / max(1.0, abs(result.objective)) * 100.0, 3,
            )
        result.seats = {key for key, var in Z.items() if solver.Value(var)}
        result.unmet_graduation_critical = sorted(
            key for key, m in miss.items() if solver.Value(m)
        )
    if result.ok and result.lp_bound is not None:
        # The LP bound is the honest distance from the true optimum; CP-SAT's
        # own bound on this structure is far looser.
        result.best_bound = min(result.best_bound, result.lp_bound)
        result.gap_pct = round(
            max(0.0, result.best_bound - result.objective)
            / max(1.0, abs(result.objective)) * 100.0, 3,
        )
    if not result.ok or result.objective < result.warm_objective:
        # Nothing better within budget — keep the warm start (a sound,
        # complete seating) rather than report failure.
        solver_status = result.status
        adopt(best, best_method)
        logger.info(
            "Exact sectioning: CP-SAT polish returned %s without beating the "
            "%s seating — keeping it", solver_status, best_method,
        )
    result.solve_time_seconds = time.monotonic() - t0
    logger.info(
        "Exact sectioning %s via %s: %d seats from %d candidates, %d overlap "
        "cliques, %d unmet graduation-critical, %.1fs",
        result.status, result.method, len(result.seats), result.n_candidates,
        n_overlap, len(result.unmet_graduation_critical), result.solve_time_seconds,
    )
    return result
