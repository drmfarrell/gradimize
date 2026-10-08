"""Warm-start hints and the optional two-phase placement solve.

A mixin composed into ``SchedulingEngine`` (see ``engine.py``). These
methods run as engine methods, so ``self`` is the full engine: they read
the data indexes it builds in ``_load_data`` (``self.course_by_code``,
``self.approved_faculty``, ``self.slot_overlaps``, …) and the per-solve
state it sets (``self._open_vars``, ``self._pinned_meetings``,
``self._repair_anchor``). Extracted from the former 3,000-line engine.py
to keep each concern in its own readable file; behavior is unchanged.
"""

import logging
from collections import defaultdict

from ortools.sat.python import cp_model

from src.core.solver_util import resolve_worker_count
from src.database.models import (
    Section,
)

logger = logging.getLogger(__name__)


class WarmStartMixin:
    """Warm-start hints and the optional two-phase placement solve."""

    def _add_placement_hint(self, model: cp_model.CpModel, sections: list[Section],
                            X: dict, Y: dict) -> int:
        """Greedy warm-start: hint a (slot, room) per meeting AND a faculty per section.

        On tight instances (staffing or rooms near capacity) CP-SAT can burn
        an entire multi-minute budget without finding ANY feasible solution.
        A rough greedy schedule — most-constrained sections first, never
        double-booking a room or an instructor across overlapping slots —
        gives the search a starting point it can repair. Staffing must be
        part of the hint: every section with approved faculty MUST be
        staffed, so a placement-only hint can anchor the search on an
        unstaffable layout and help nothing.

        Hints are advisory: a bad hint costs nothing, a decent one cuts
        first-feasible time from minutes to seconds. Enrollment (Z) is left
        unhinted; it propagates quickly once placements are anchored.
        Disable with solver_settings["placement_hint"] = false.
        """
        if not self.config.solver_settings.get("placement_hint", True):
            return 0

        # Cross-listed mirrors follow their leader via equality constraints;
        # hinting them separately would fight the room-occupancy greedy.
        xl_secondary = self._crosslist_secondary(sections)
        by_meeting: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
        for (crn, k, slot_id, room_id) in X:
            if crn in xl_secondary:
                continue
            by_meeting[crn, k].append((slot_id, room_id))
        meetings_by_crn: dict[str, list[int]] = defaultdict(list)
        for (crn, k) in by_meeting:
            meetings_by_crn[crn].append(k)

        # Most-constrained sections first (fewest total placement options).
        section_order = sorted(
            meetings_by_crn,
            key=lambda crn: (
                sum(len(by_meeting[crn, k]) for k in meetings_by_crn[crn]),
                crn,
            ),
        )
        course_of = {s.crn: s.course_code for s in sections}
        # Dedicated (explicit approved list) instructors before pool ones, so
        # any-faculty fallbacks stay free for courses that truly need them.
        dedicated = {f.id for f in self.faculty if f.approved_courses}

        room_used: set[tuple[str, int]] = set()   # (room, slot) + overlap shadows
        fac_used: set[tuple[str, int]] = set()    # (faculty, slot) + overlap shadows

        def slot_free(chosen: list[int], slot_id: int) -> bool:
            return slot_id not in chosen and not any(
                sl in chosen for sl in self.slot_overlaps.get(slot_id, ())
            )

        def fac_free(fid: str, slots: list[int]) -> bool:
            return not any(
                (fid, sl) in fac_used
                or any((fid, osl) in fac_used
                       for osl in self.slot_overlaps.get(sl, ()))
                for sl in slots
            )

        def try_section(crn, first_option) -> tuple[list, str] | None:
            """Place all meetings starting from first_option; find staffing.

            Returns (placements, faculty_id_or_None) or None if it doesn't
            work out — the caller then tries a different first placement.
            """
            placements = []
            chosen: list[int] = []
            ks = sorted(meetings_by_crn[crn])
            for idx, k in enumerate(ks):
                options = [first_option] if idx == 0 else sorted(by_meeting[crn, k])
                placed = None
                for slot_id, room_id in options:
                    if (room_id, slot_id) in room_used or not slot_free(chosen, slot_id):
                        continue
                    placed = (k, slot_id, room_id)
                    break
                if placed is None:
                    return None
                placements.append(placed)
                chosen.append(placed[1])

            candidates = sorted(
                (f.id for f in self.approved_faculty.get(course_of[crn], [])
                 if (crn, f.id) in Y),
                key=lambda fid: (fid not in dedicated, fid),
            )
            for fid in candidates:
                if fac_free(fid, chosen):
                    return placements, fid
            # No instructor free anywhere — only usable if the course has no
            # approved-faculty requirement at all (no Y vars).
            return (placements, None) if not candidates else None

        chosen_by_crn: dict[str, tuple[list, str | None]] = {}
        for crn in section_order:
            ks = sorted(meetings_by_crn[crn])
            result = None
            for first_option in sorted(by_meeting[crn, ks[0]]):
                result = try_section(crn, first_option)
                if result is not None:
                    break
            if result is None:
                continue
            placements, fid = result
            chosen_by_crn[crn] = (placements, fid)
            for _k, slot_id, room_id in placements:
                room_used.add((room_id, slot_id))
                for overlap_slot in self.slot_overlaps.get(slot_id, ()):
                    room_used.add((room_id, overlap_slot))
            if fid is not None:
                for _, slot_id, _ in placements:
                    fac_used.add((fid, slot_id))
                    for overlap_slot in self.slot_overlaps.get(slot_id, ()):
                        fac_used.add((fid, overlap_slot))

        # Siblings are interchangeable, so hand the greedy's picks out among
        # them in the canonical order the symmetry-breaking constraints
        # expect — otherwise the hint violates them and is wasted.
        chosen_by_crn = self._canonicalize_sibling_hints(sections, chosen_by_crn)

        hinted_meetings = 0
        hinted_faculty = 0
        open_vars = self._open_vars
        for crn, (placements, fid) in chosen_by_crn.items():
            for k, slot_id, room_id in placements:
                if (crn, k, slot_id, room_id) in X:
                    model.AddHint(X[crn, k, slot_id, room_id], 1)
                    hinted_meetings += 1
            if fid is not None and (crn, fid) in Y:
                model.AddHint(Y[crn, fid], 1)
                hinted_faculty += 1
            if open_vars is not None and crn in open_vars:
                model.AddHint(open_vars[crn], 1)

        logger.info(
            f"Placement hint: {hinted_meetings}/{len(by_meeting)} meetings and "
            f"{hinted_faculty}/{len(meetings_by_crn)} section staffings pre-placed"
        )
        return hinted_meetings

    def _canonicalize_sibling_hints(
        self, sections: list[Section], chosen_by_crn: dict[str, tuple[list, "str | None"]]
    ) -> dict[str, tuple[list, "str | None"]]:
        """Permute hint picks within each sibling group into the slot order
        that _add_sibling_symmetry_breaking enforces (meeting-0 slot index
        non-decreasing; unplaced siblings last). Valid because siblings
        share identical X/Y option sets. No-op when symmetry breaking is off.
        """
        if not self.config.solver_settings.get("symmetry_breaking", True):
            return chosen_by_crn
        slot_index = self._slot_order_index()

        def order_key(pick: tuple[list, "str | None"]) -> int:
            placements, _fid = pick
            first = [slot_index.get(slot_id, 0) for k, slot_id, _r in placements if k == 0]
            return first[0] if first else 10**9

        out = dict(chosen_by_crn)
        for group in self._sibling_groups(sections):
            picks = sorted(
                (chosen_by_crn[s.crn] for s in group if s.crn in chosen_by_crn),
                key=order_key,
            )
            if not picks:
                continue
            for s in group:
                out.pop(s.crn, None)
            for s, pick in zip(group, picks):
                out[s.crn] = pick
        return out

    def _solve_placement_phase(
        self, sections: list[Section], time_budget: float
    ) -> "tuple[set, set] | None":
        """Phase 1 of a two-phase solve: placement/staffing only, no enrollment.

        Builds a model with just the X/Y hard constraints (assignment, room
        and faculty no-overlap, linked courses, intra-section, travel) and no
        objective — pure feasibility, which CP-SAT settles orders of magnitude
        faster than the full model. Returns the chosen (X-key, Y-key) sets,
        or None if nothing was found in the budget (the caller then falls
        back to the ordinary single-phase solve, so a phase-1 miss costs only
        its time slice).
        """
        model = cp_model.CpModel()
        # Phase 1 keeps every section open — closure decisions need the
        # enrollment side, which this phase deliberately omits. The main
        # model may already have been built against real open vars, so
        # save/restore rather than clobber.
        saved_open_vars = self._open_vars
        self._open_vars = None
        try:
            # Z vars are created too but stay unconstrained — presolve drops
            # free variables instantly, and reusing _create_variables
            # unchanged beats forking it.
            X, Y, _Z = self._create_variables(model, sections)
            section_in_slot = self._compute_section_in_slot(model, sections, X)
            self._add_assignment_constraints(model, sections, X, Y)
            self._add_room_constraints(model, sections, X)
            self._add_faculty_constraints(model, sections, X, Y, section_in_slot)
            self._add_linked_course_constraints(model, sections, X, section_in_slot)
            self._add_intra_section_constraints(model, sections, X, section_in_slot)
            self._add_faculty_travel_time_constraints(model, sections, X, Y)
            self._add_crosslist_link_constraints(model, sections, X, Y)
            # Same sibling order as the full model, so the phase-1 solution
            # is a valid hint for it.
            self._add_sibling_symmetry_breaking(model, sections, section_in_slot)
            self._add_placement_hint(model, sections, X, Y)
        finally:
            self._open_vars = saved_open_vars

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_budget
        solver.parameters.num_workers = resolve_worker_count(
            self.config.solver_settings["num_workers"]
        )
        solver.parameters.log_search_progress = False
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            logger.info(
                "Placement phase found no solution (%s) in %.0fs — "
                "continuing single-phase",
                solver.StatusName(status), time_budget,
            )
            return None
        x_keys = {k for k, v in X.items() if solver.Value(v) == 1}
        y_keys = {k for k, v in Y.items() if solver.Value(v) == 1}
        logger.info(
            "Placement phase solved (%s): %d placements, %d staffings",
            solver.StatusName(status), len(x_keys), len(y_keys),
        )
        return x_keys, y_keys

    def _hint_from_placement(
        self, model: cp_model.CpModel, sections: list[Section],
        X: dict, Y: dict, Z: dict, x_keys: set, y_keys: set,
    ) -> int:
        """Feed a solved placement into the full model as a warm start.

        Hints every placed X/Y at 1, then greedily seeds enrollment:
        per course, highest-urgency demands first, first-fit into sections
        up to each section's effective capacity under the current capacity
        mode. The result is a near-complete solution the full solve can
        polish instead of hunting for feasibility from scratch.
        """
        for key in x_keys:
            model.AddHint(X[key], 1)
        for key in y_keys:
            model.AddHint(Y[key], 1)

        sec_cfg = self.config.section_settings
        float_capacity = sec_cfg.get("capacity_mode", "fixed") == "room"
        global_cap = sec_cfg.get("max_class_size")
        respect_rooms = self.config.hard_constraints.get("respect_room_capacity", True)

        room_caps_by_crn: dict[str, list[int]] = defaultdict(list)
        if respect_rooms:
            for (crn, _k, _slot_id, room_id) in x_keys:
                room = self.room_by_id.get(room_id)
                if room is not None:
                    room_caps_by_crn[crn].append(room.capacity)

        remaining: dict[str, int] = {}
        sections_by_course: dict[str, list[Section]] = defaultdict(list)
        for s in sections:
            sections_by_course[s.course_code].append(s)
            course = self.course_by_code[s.course_code]
            room_min = min(room_caps_by_crn[s.crn]) if room_caps_by_crn.get(s.crn) else None
            if float_capacity:
                ceiling = course.max_class_size or global_cap
                cap = room_min if room_min is not None else (ceiling or s.capacity)
                if ceiling and room_min is not None:
                    cap = min(cap, ceiling)
            else:
                cap = s.capacity if room_min is None else min(s.capacity, room_min)
            remaining[s.crn] = max(0, cap)

        demands_by_course: dict[str, list] = defaultdict(list)
        for d in self.demands:
            demands_by_course[d.course_code].append(d)

        hinted = 0
        for code, course_demands in demands_by_course.items():
            course_sections = sections_by_course.get(code, [])
            if not course_sections:
                continue
            for d in sorted(course_demands, key=lambda d: -d.urgency_score):
                for s in course_sections:
                    if remaining[s.crn] > 0 and (d.student_id, s.crn) in Z:
                        model.AddHint(Z[d.student_id, s.crn], 1)
                        remaining[s.crn] -= 1
                        hinted += 1
                        break

        logger.info(
            "Warm start from placement: %d placements, %d staffings, "
            "%d enrollments hinted",
            len(x_keys), len(y_keys), hinted,
        )
        return hinted

    def _hint_from_anchor(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        X: dict,
        Y: dict,
        Z: dict,
    ) -> int:
        """Warm-start solve() with the repair anchor (the base schedule).

        Hints every still-valid base placement/staffing/seat at 1. Unless the
        disruption is severe this is a near-complete first solution, so the
        solver spends its budget proving minimal change rather than hunting
        for feasibility.
        """
        anchor = self._repair_anchor or {}
        hinted = 0
        for section in sections:
            base = anchor.get(section.crn)
            if not base:
                continue
            for k, (base_slot, base_room) in enumerate(base["meetings"]):
                key = (section.crn, k, base_slot, base_room)
                if key in X:
                    model.AddHint(X[key], 1)
                    hinted += 1
            base_fid = base.get("faculty_id")
            if base_fid and (section.crn, base_fid) in Y:
                model.AddHint(Y[section.crn, base_fid], 1)
                hinted += 1
            for sid in base.get("students", ()):
                if (sid, section.crn) in Z:
                    model.AddHint(Z[sid, section.crn], 1)
                    hinted += 1
            open_vars = self._open_vars
            if open_vars is not None and section.crn in open_vars:
                model.AddHint(open_vars[section.crn], 1)
        logger.info("Repair warm start: %d hints from base schedule", hinted)
        return hinted
