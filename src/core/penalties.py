"""Soft-penalty builders and the objective assembly.

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
from typing import Any

from ortools.sat.python import cp_model

from src.database.models import (
    Section,
)

logger = logging.getLogger(__name__)


def _and_indicator(model: cp_model.CpModel, name: str, a: Any, b: Any, weight: int) -> Any:
    """A BoolVar standing in for ``a AND b`` inside an objective term of sign ``weight``.

    Only the direction the objective cannot exploit needs a constraint: a
    penalized indicator (weight < 0) must be forced UP when both are true
    (the solver already wants it 0); a rewarded one (weight > 0) must be
    forced DOWN unless both are true. The old two-sided linearization added
    a redundant constraint per indicator — tens of thousands at scale.
    """
    ind = model.NewBoolVar(name)
    if weight <= 0:
        model.Add(ind >= a + b - 1)
    if weight >= 0:
        model.Add(2 * ind <= a + b)
    return ind


class PenaltyMixin:
    """Soft-penalty builders and the objective assembly."""

    def _add_faculty_quality_penalties(
        self, model: cp_model.CpModel, sections: list[Section],
        Y: dict, section_in_slot: dict,
    ) -> list:
        """Soft terms for instructor-friendly schedules: preps and days.

        - Prep penalty: each DISTINCT course an instructor teaches beyond
          their first costs faculty_prep_penalty.weight — three sections of
          one course is a far lighter load than three different courses.
        - Teaching-day penalty: each DISTINCT day an instructor teaches
          beyond their first costs faculty_teaching_day_penalty.weight —
          the classic "teach TR, write MWF" pattern.

        Cross-listed mirrors are excluded (one combined class is one prep,
        one time block). Returns (weight, var) penalty terms.
        """
        prep_w = int(self.config.soft_constraints
                     .get("faculty_prep_penalty", {}).get("weight", 0))
        day_w = int(self.config.soft_constraints
                    .get("faculty_teaching_day_penalty", {}).get("weight", 0))
        if not prep_w and not day_w:
            return []

        xl_secondary = self._crosslist_secondary(sections)
        slot_days = {s.id: list(s.days) for s in self.time_slots}
        terms: list = []
        preps_built = 0
        day_vars_built = 0

        sections_by_course: dict[str, list[Section]] = defaultdict(list)
        for s in sections:
            if s.crn not in xl_secondary:
                sections_by_course[s.course_code].append(s)

        for faculty in self.faculty:
            # Course-level "teaches" indicators for the prep penalty.
            if prep_w:
                teach_vars = []
                for code, course_sections in sections_by_course.items():
                    y_vars = [
                        Y[s.crn, faculty.id]
                        for s in course_sections
                        if (s.crn, faculty.id) in Y
                    ]
                    if not y_vars:
                        continue
                    t = model.NewBoolVar(f"prep_{faculty.id}_{code}")
                    for y in y_vars:
                        model.AddImplication(y, t)
                    model.Add(t <= sum(y_vars))
                    teach_vars.append(t)
                if len(teach_vars) > 1:
                    # Penalize distinct courses beyond the first: the "any"
                    # refund keeps a single-prep instructor at zero cost.
                    any_t = model.NewBoolVar(f"prep_any_{faculty.id}")
                    for t in teach_vars:
                        model.Add(any_t >= t)
                    model.Add(any_t <= sum(teach_vars))
                    for t in teach_vars:
                        terms.append((prep_w, t))
                    terms.append((-prep_w, any_t))
                    preps_built += len(teach_vars)

            # Day indicators for the teaching-day penalty. Lower-bound
            # implications only: the negative weight makes the solver keep
            # a day at 0 unless some (assigned, in-slot-on-day) pair forces
            # it to 1 — no upper bound needed.
            if day_w:
                day_forced: dict[str, list] = defaultdict(list)
                for s in sections:
                    if s.crn in xl_secondary or (s.crn, faculty.id) not in Y:
                        continue
                    y = Y[s.crn, faculty.id]
                    course = self.course_by_code[s.course_code]
                    for k in range(len(self.course_meetings.get(course.code, []))):
                        for slot_id, days in slot_days.items():
                            sis = section_in_slot.get((s.crn, k, slot_id))
                            if sis is None:
                                continue
                            for d in days:
                                day_forced[d].append((sis, y))
                day_list = []
                for d, pairs in day_forced.items():
                    dv = model.NewBoolVar(f"day_{faculty.id}_{d}")
                    for sis, y in pairs:
                        model.Add(dv >= sis + y - 1)
                    day_list.append(dv)
                if len(day_list) > 1:
                    any_d = model.NewBoolVar(f"day_any_{faculty.id}")
                    for dv in day_list:
                        model.Add(any_d >= dv)
                    model.Add(any_d <= sum(day_list))
                    for dv in day_list:
                        terms.append((day_w, dv))
                    terms.append((-day_w, any_d))
                    day_vars_built += len(day_list)

        logger.info(
            f"Faculty quality penalties: {preps_built} prep indicators, "
            f"{day_vars_built} day indicators"
        )
        return terms

    def _build_conflict_soft_penalties(
        self, model: cp_model.CpModel, sections: list[Section],
        section_in_slot: dict,
    ) -> list:
        """Build soft penalty terms for high-conflict course pairs in overlapping slots.

        Uses course-level slot indicators (not section-level) for ~25x fewer variables than the
        per-section approach. For each high-conflict pair (C1, C2), penalizes when any section of
        C1 and any section of C2 are assigned to overlapping time slots.

        Returns list of (weight, BoolVar) penalty terms for the objective (all weights are
        negative).
        """
        threshold = self.config.conflict_urgency_threshold
        high_conflict_pairs = self.conflict_matrix.get_high_conflict_pairs(threshold)
        if not high_conflict_pairs:
            return []

        # Build crn → course_code lookup
        crn_to_course = {s.crn: s.course_code for s in sections}

        # Build course_slot_sis[course_code][slot_id] = list of section_in_slot vars
        course_slot_sis: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
        for (crn, k, slot_id), sis_var in section_in_slot.items():
            cc = crn_to_course.get(crn)
            if cc:
                course_slot_sis[cc][slot_id].append(sis_var)

        # Build c_at_slot[(course_code, slot_id)] = BoolVar: is any section of course in this slot?
        c_at_slot: dict[tuple[str, int], Any] = {}
        for cc, slot_sis_map in course_slot_sis.items():
            for slot_id, sis_vars in slot_sis_map.items():
                if len(sis_vars) == 1:
                    c_at_slot[(cc, slot_id)] = sis_vars[0]  # reuse directly
                else:
                    ind = model.NewBoolVar(f"cat_{cc}_{slot_id}")
                    for sis in sis_vars:
                        model.AddImplication(sis, ind)  # if any section is here, indicator = 1
                    model.Add(ind <= sum(sis_vars))       # indicator = 0 if no section here
                    c_at_slot[(cc, slot_id)] = ind

        # Pre-group by course for efficient lookup
        c_at_slot_by_course: dict[str, dict[int, Any]] = defaultdict(dict)
        for (cc, slot_id), ind in c_at_slot.items():
            c_at_slot_by_course[cc][slot_id] = ind

        # Build course-pair overlap indicators and penalty terms
        penalty_terms = []
        indicators_added = 0

        # A cross-listed pair is ONE class — never penalize it for
        # "overlapping" with itself.
        group_of = {
            c.code: c.crosslist_group
            for c in self.courses
            if c.crosslist_group
        }

        for c1, c2, score in high_conflict_pairs:
            if group_of.get(c1) is not None and group_of.get(c1) == group_of.get(c2):
                continue
            weight = max(1, int(score))
            c1_slots = c_at_slot_by_course.get(c1, {})
            c2_slots = c_at_slot_by_course.get(c2, {})
            if not c1_slots or not c2_slots:
                continue

            for slot1_id, ind1 in c1_slots.items():
                overlapping = self.slot_overlaps.get(slot1_id, set()) | {slot1_id}
                for slot2_id in overlapping:
                    ind2 = c2_slots.get(slot2_id)
                    if ind2 is None:
                        continue
                    both = _and_indicator(
                        model, f"cov_{c1}_{slot1_id}_{c2}_{slot2_id}", ind1, ind2, -weight
                    )
                    penalty_terms.append((-weight, both))
                    indicators_added += 1

        logger.info(f"Conflict soft penalties: {indicators_added} course-level overlap indicators")
        return penalty_terms

    def _add_repair_anchor_terms(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        X: dict,
        Y: dict,
        Z: dict,
        section_in_slot: dict,
    ) -> list:
        """Stability rewards toward the repair anchor, as (weight, var) terms.

        Rides existing variables only — no new model variables:
        - time:       section_in_slot[crn, k, base_slot]
        - room:       X[crn, k, slot, base_room] summed over slots (≤1 can hold)
        - instructor: Y[crn, base_faculty]
        - seat:       Z[student, crn] for each base enrollment
        A base assignment that no longer exists (slot removed, room
        deactivated, instructor gone, demand dropped) simply contributes no
        term: the forced change is free, so the solver repairs it while
        everything else stays put. Pinned sections are hard-fixed already and
        crosslist secondaries mirror their leader; both are skipped.
        """
        anchor = self._repair_anchor or {}
        cfg = self.config.soft_constraints.get("repair_anchor", {})
        w_time = int(cfg.get("time", 50000))
        w_room = int(cfg.get("room", 10000))
        w_fac = int(cfg.get("instructor", 30000))
        w_seat = int(cfg.get("seat", 100))
        xl_secondary = self._crosslist_secondary(sections)

        terms: list = []
        n_anchored = 0
        for section in sections:
            base = anchor.get(section.crn)
            if not base or section.is_pinned or section.crn in xl_secondary:
                continue
            n_anchored += 1
            course = self.course_by_code[section.course_code]
            meetings = self.course_meetings.get(course.code, [])
            for k, (base_slot, base_room) in enumerate(base["meetings"]):
                if k >= len(meetings):
                    break
                if w_time and (section.crn, k, base_slot) in section_in_slot:
                    terms.append((w_time, section_in_slot[section.crn, k, base_slot]))
                if w_room:
                    for slot in meetings[k].compatible_slots:
                        key = (section.crn, k, slot.id, base_room)
                        if key in X:
                            terms.append((w_room, X[key]))
            base_fid = base.get("faculty_id")
            if w_fac and base_fid and (section.crn, base_fid) in Y:
                terms.append((w_fac, Y[section.crn, base_fid]))
            if w_seat:
                for sid in base.get("students", ()):
                    if (sid, section.crn) in Z:
                        terms.append((w_seat, Z[sid, section.crn]))
        logger.info(
            "Repair anchor: %d sections anchored, %d stability terms",
            n_anchored, len(terms),
        )
        return terms

    def _add_back_to_back_penalties(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        X: dict,
        Y: dict,
        section_in_slot: dict[tuple[str, int, int], Any],
    ) -> list:
        """Add penalties for faculty back-to-back teaching.

        Returns list of objective terms.
        """
        b2b_config = self.config.soft_constraints.get("back_to_back_penalty", {})
        faculty_config = b2b_config.get("faculty", {})
        weight = faculty_config.get("weight", -20)

        if weight == 0:
            return []

        objective_terms = []

        for faculty in self.faculty:
            prefs = faculty.preferences or {}
            if prefs.get("allow_back_to_back", False):
                continue

            # Build faculty_in_slot[slot_id] indicators
            faculty_in_slot: dict[int, Any] = {}

            for slot in self.time_slots:
                slot_indicators = []
                for section in sections:
                    if (section.crn, faculty.id) not in Y:
                        continue
                    course = self.course_by_code[section.course_code]
                    meetings = self.course_meetings.get(course.code, [])
                    for k, meeting in enumerate(meetings):
                        if (section.crn, k, slot.id) not in section_in_slot:
                            continue
                        # faculty teaches this section AND section is in this slot
                        sis = section_in_slot[section.crn, k, slot.id]
                        y_var = Y[section.crn, faculty.id]
                        indicator = _and_indicator(
                            model, f"fis_{faculty.id}_{section.crn}_{k}_{slot.id}",
                            sis, y_var, weight,
                        )
                        slot_indicators.append(indicator)

                if not slot_indicators:
                    continue

                if len(slot_indicators) == 1:
                    faculty_in_slot[slot.id] = slot_indicators[0]
                else:
                    agg = model.NewBoolVar(f"fis_agg_{faculty.id}_{slot.id}")
                    model.AddMaxEquality(agg, slot_indicators)
                    faculty_in_slot[slot.id] = agg

            # Penalize adjacent slot pairs where faculty is teaching in both
            for s1_id, adj_set in self.slot_adjacencies.items():
                if s1_id not in faculty_in_slot:
                    continue
                for s2_id in adj_set:
                    if s2_id <= s1_id:
                        continue  # avoid double-counting
                    if s2_id not in faculty_in_slot:
                        continue
                    fis1 = faculty_in_slot[s1_id]
                    fis2 = faculty_in_slot[s2_id]
                    both = _and_indicator(
                        model, f"b2b_{faculty.id}_{s1_id}_{s2_id}", fis1, fis2, weight
                    )
                    objective_terms.append(weight * both)

        return objective_terms

    def _add_day_balance_penalties(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        section_in_slot: dict[tuple[str, int, int], Any],
    ) -> list:
        """Add penalties for uneven section distribution across days.

        Returns list of objective terms.
        """
        db_config = self.config.soft_constraints.get("day_balance_penalty", {})
        weight = db_config.get("weight", -15)

        if weight == 0:
            return []

        days = ["M", "T", "W", "R", "F"]
        num_days = len(days)

        # Map slot_id -> set of days
        slot_days: dict[int, set[str]] = {}
        for slot in self.time_slots:
            slot_days[slot.id] = set(slot.days)

        # Collect all section_in_slot vars
        all_sis_vars = list(section_in_slot.values())
        if not all_sis_vars:
            return []

        # total = sum of all section_in_slot (total meeting-slot assignments)
        total = model.NewIntVar(0, len(all_sis_vars), "day_bal_total")
        model.Add(total == sum(all_sis_vars))

        objective_terms = []

        for day in days:
            # Collect section_in_slot vars for slots on this day
            day_vars = []
            for (crn, k, slot_id), var in section_in_slot.items():
                if day in slot_days.get(slot_id, set()):
                    day_vars.append(var)

            day_count = model.NewIntVar(0, len(day_vars) if day_vars else 0, f"day_count_{day}")
            if day_vars:
                model.Add(day_count == sum(day_vars))
            else:
                model.Add(day_count == 0)

            # Scaled deviation: |day_count * num_days - total|
            diff = model.NewIntVar(
                -len(all_sis_vars) * num_days,
                len(all_sis_vars) * num_days,
                f"day_diff_{day}",
            )
            model.Add(diff == day_count * num_days - total)

            abs_dev = model.NewIntVar(0, len(all_sis_vars) * num_days, f"day_abs_{day}")
            model.AddAbsEquality(abs_dev, diff)

            objective_terms.append(weight * abs_dev)

        return objective_terms

    def _add_room_balance_penalties(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        X: dict,
    ) -> list:
        """Penalize uneven section distribution across rooms.

        Encourages spreading sections across available rooms rather than
        clustering into a few, improving room utilization.
        """
        weight = self.config.soft_constraints.get("room_balance_penalty", {}).get("weight", -5)
        if weight == 0:
            return []

        # Index X vars by room
        x_by_room: dict[str, list] = defaultdict(list)
        for (crn, k, slot_id, room_id), var in X.items():
            x_by_room[room_id].append(var)

        active_rooms = {rid: vars for rid, vars in x_by_room.items() if vars}
        if len(active_rooms) <= 1:
            return []

        num_rooms = len(active_rooms)
        total_x = sum(len(v) for v in active_rooms.values())

        # Total sections assigned (each meeting gets exactly one room)
        total = model.NewIntVar(0, total_x, "room_bal_total")
        total_vars = []
        room_counts = {}
        for room_id, vars_list in active_rooms.items():
            rc = model.NewIntVar(0, len(vars_list), f"room_count_{room_id}")
            model.Add(rc == sum(vars_list))
            room_counts[room_id] = rc
            total_vars.extend(vars_list)
        model.Add(total == sum(total_vars))

        objective_terms = []
        for room_id, rc in room_counts.items():
            diff = model.NewIntVar(-total_x * num_rooms, total_x * num_rooms,
                                   f"room_diff_{room_id}")
            model.Add(diff == rc * num_rooms - total)
            abs_dev = model.NewIntVar(0, total_x * num_rooms, f"room_abs_{room_id}")
            model.AddAbsEquality(abs_dev, diff)
            objective_terms.append(weight * abs_dev)

        return objective_terms

    def _add_faculty_load_balance_penalties(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        Y: dict,
    ) -> list:
        """Penalize uneven credit-hour distribution across faculty.

        Encourages balanced teaching loads rather than overloading some
        faculty while others are underutilized.
        """
        weight = self.config.soft_constraints.get("faculty_load_balance_penalty", {}).get(
            "weight", -8
        )
        if weight == 0:
            return []

        # Compute each faculty's credit load as an IntVar
        faculty_loads: dict[str, Any] = {}
        for faculty in self.faculty:
            load_terms = []
            for section in sections:
                if (section.crn, faculty.id) not in Y:
                    continue
                course = self.course_by_code[section.course_code]
                load_terms.append(course.credits * Y[section.crn, faculty.id])
            if not load_terms:
                continue
            # Domain must be wide enough to hold any feasible assignment;
            # use total credits across all sections this faculty could teach.
            max_possible = sum(
                self.course_by_code[s.course_code].credits
                for s in sections if (s.crn, faculty.id) in Y
            )
            fload = model.NewIntVar(0, max_possible, f"fload_{faculty.id}")
            model.Add(fload == sum(load_terms))
            faculty_loads[faculty.id] = fload

        if len(faculty_loads) <= 1:
            return []

        num_fac = len(faculty_loads)
        max_total = sum(
            sum(self.course_by_code[s.course_code].credits for s in sections if (s.crn, f.id) in Y)
            for f in self.faculty if f.id in faculty_loads
        )

        total_load = model.NewIntVar(0, max_total, "fload_total")
        model.Add(total_load == sum(faculty_loads.values()))

        objective_terms = []
        for fid, fload in faculty_loads.items():
            diff = model.NewIntVar(-max_total * num_fac, max_total * num_fac,
                                   f"fload_diff_{fid}")
            model.Add(diff == fload * num_fac - total_load)
            abs_dev = model.NewIntVar(0, max_total * num_fac, f"fload_abs_{fid}")
            model.AddAbsEquality(abs_dev, diff)
            objective_terms.append(weight * abs_dev)

        return objective_terms

    def _set_objective(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        X: dict,
        Y: dict,
        Z: dict,
        section_in_slot: dict,
        conflict_penalty_terms: list | None = None,
    ):
        """Set the objective function: maximize weighted student enrollment minus conflict
        penalties.
        """
        objective_terms = []

        # Per-open-section cost (only with section closure enabled): a
        # gentle consolidation pressure toward fewer, fuller sections.
        open_vars = self._open_vars
        if open_vars is not None:
            open_weight = int(
                self.config.soft_constraints
                .get("open_section_penalty", {}).get("weight", -5)
            )
            if open_weight:
                for var in open_vars.values():
                    objective_terms.append(open_weight * var)

        # Primary: maximize urgency-weighted enrollment
        for demand in self.demands:
            course_sections = [s for s in sections if s.course_code == demand.course_code]
            for section in course_sections:
                if (demand.student_id, section.crn) in Z:
                    # Multiply by 100 and convert to int for CP-SAT
                    int_urgency = int(demand.urgency_score * 100)
                    objective_terms.append(int_urgency * Z[demand.student_id, section.crn])

        # Secondary: soft constraint penalties (smaller weights)
        # Penalties apply to meeting 0 (primary meeting) only for simplicity
        early_hour = self.config.soft_constraints["early_morning_penalty"]["hour"]
        early_weight = self.config.soft_constraints["early_morning_penalty"]["weight"]
        late_hour = self.config.soft_constraints["late_afternoon_penalty"]["hour"]
        late_weight = self.config.soft_constraints["late_afternoon_penalty"]["weight"]

        for section in sections:
            course = self.course_by_code[section.course_code]
            meetings = self.course_meetings.get(course.code, [])
            for k, meeting in enumerate(meetings):
                for slot in meeting.compatible_slots:
                    start_hour = int(slot.start_time.split(":")[0])
                    end_hour = int(slot.end_time.split(":")[0])
                    for room in meeting.compatible_rooms:
                        if (section.crn, k, slot.id, room.id) in X:
                            if start_hour < early_hour:
                                objective_terms.append(
                                    early_weight * X[section.crn, k, slot.id, room.id]
                                )
                            if end_hour >= late_hour:
                                objective_terms.append(
                                    late_weight * X[section.crn, k, slot.id, room.id]
                                )

        # Faculty preferred slot bonus
        pref_weight = self.config.soft_constraints["faculty_preference_bonus"]["weight"]
        for faculty in self.faculty:
            preferred = set(faculty.preferences.get("preferred_slots", []))
            if not preferred:
                continue
            for section in sections:
                if (section.crn, faculty.id) not in Y:
                    continue
                course = self.course_by_code[section.course_code]
                meetings = self.course_meetings.get(course.code, [])
                for k, meeting in enumerate(meetings):
                    for slot in meeting.compatible_slots:
                        if slot.id in preferred:
                            for room in meeting.compatible_rooms:
                                if (section.crn, k, slot.id, room.id) in X:
                                    x_var = X[section.crn, k, slot.id, room.id]
                                    y_var = Y[section.crn, faculty.id]
                                    indicator = _and_indicator(
                                        model,
                                        f"pref_{section.crn}_{k}_{slot.id}_{faculty.id}",
                                        x_var, y_var, pref_weight,
                                    )
                                    objective_terms.append(pref_weight * indicator)

        # Back-to-back and day balance penalties
        objective_terms.extend(
            self._add_back_to_back_penalties(model, sections, X, Y, section_in_slot)
        )
        objective_terms.extend(
            self._add_day_balance_penalties(model, sections, section_in_slot)
        )
        objective_terms.extend(
            self._add_room_balance_penalties(model, sections, X)
        )
        objective_terms.extend(
            self._add_faculty_load_balance_penalties(model, sections, Y)
        )

        # Soft conflict penalties: penalize sections of high-conflict pairs in overlapping slots
        if conflict_penalty_terms:
            for weight, bool_var in conflict_penalty_terms:
                objective_terms.append(weight * bool_var)

        model.Maximize(sum(objective_terms))
