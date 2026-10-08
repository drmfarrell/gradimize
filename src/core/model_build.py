"""Hard-constraint & variable builders for the CP-SAT schedule model.

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
from itertools import combinations
from typing import Any

from ortools.sat.python import cp_model

from src.database.models import (
    Section,
    StructureType,
)

logger = logging.getLogger(__name__)


class ModelBuildMixin:
    """Hard-constraint & variable builders for the CP-SAT schedule model."""

    def _create_variables(
        self, model: cp_model.CpModel, sections: list[Section]
    ) -> tuple[dict, dict, dict]:
        """Create decision variables.

        X[crn, k, slot_id, room_id] = 1 if section's meeting k is in slot/room
        Y[crn, faculty_id] = 1 if section is taught by faculty
        Z[student_id, crn] = 1 if student is enrolled in section
        """
        X = {}
        Y = {}
        Z = {}

        for section in sections:
            course = self.course_by_code[section.course_code]
            meetings = self.course_meetings.get(course.code, [])

            # X variables: per-meeting section-slot-room assignments
            for k, meeting in enumerate(meetings):
                for slot in meeting.compatible_slots:
                    for room in meeting.compatible_rooms:
                        var_name = f"X_{section.crn}_{k}_{slot.id}_{room.id}"
                        X[section.crn, k, slot.id, room.id] = model.NewBoolVar(var_name)

            # Y variables: section-faculty assignments (unchanged, per-section)
            for faculty in self.approved_faculty.get(course.code, []):
                var_name = f"Y_{section.crn}_{faculty.id}"
                Y[section.crn, faculty.id] = model.NewBoolVar(var_name)

        # Z variables: student-section enrollments
        for course_code, demands in self.demands_by_course.items():
            course_sections = [s for s in sections if s.course_code == course_code]
            for demand in demands:
                for section in course_sections:
                    var_name = f"Z_{demand.student_id}_{section.crn}"
                    Z[demand.student_id, section.crn] = model.NewBoolVar(var_name)

        logger.info(f"Created {len(X)} X vars, {len(Y)} Y vars, {len(Z)} Z vars")
        return X, Y, Z

    def _add_assignment_constraints(
        self, model: cp_model.CpModel, sections: list[Section], X: dict, Y: dict
    ):
        """Each section's meetings must each be assigned exactly one (slot, room), and one faculty.

        With section closure enabled (self._open_vars), "exactly one"
        becomes "exactly open": a closed section gets no placement and no
        instructor. Pinned sections are always open.
        """
        open_vars = self._open_vars
        for section in sections:
            course = self.course_by_code[section.course_code]
            meetings = self.course_meetings.get(course.code, [])

            if section.is_pinned and open_vars is not None:
                model.Add(open_vars[section.crn] == 1)

            if section.is_pinned:
                # Get pinned meeting data (from re-solve or fall back to section fields)
                pinned_meeting_data = self._pinned_meetings.get(section.crn)

                for k, meeting in enumerate(meetings):
                    # Determine pinned slot+room for this meeting
                    pinned_slot_id = None
                    pinned_room_id = None

                    if pinned_meeting_data and k < len(pinned_meeting_data):
                        pinned_slot_id, pinned_room_id = pinned_meeting_data[k]

                    if pinned_slot_id and pinned_room_id:
                        if (section.crn, k, pinned_slot_id, pinned_room_id) in X:
                            model.Add(X[section.crn, k, pinned_slot_id, pinned_room_id] == 1)
                            # Zero out all other X vars for this meeting
                            for s in meeting.compatible_slots:
                                for r in meeting.compatible_rooms:
                                    if (section.crn, k, s.id, r.id) in X and (s.id, r.id) != (
                                        pinned_slot_id,
                                        pinned_room_id,
                                    ):
                                        model.Add(X[section.crn, k, s.id, r.id] == 0)
                        else:
                            # The pinned placement no longer exists (room
                            # deactivated, slot newly blocked, …). Zeroing the
                            # alternatives with no ==1 used to silently leave
                            # the meeting unscheduled; re-assign it instead.
                            logger.warning(
                                "Pinned placement (slot %s, room %s) for section "
                                "%s meeting %d is no longer available; "
                                "re-assigning that meeting normally",
                                pinned_slot_id, pinned_room_id, section.crn, k,
                            )
                            slot_room_vars = [
                                X[section.crn, k, s.id, r.id]
                                for s in meeting.compatible_slots
                                for r in meeting.compatible_rooms
                                if (section.crn, k, s.id, r.id) in X
                            ]
                            if slot_room_vars:
                                model.Add(sum(slot_room_vars) == 1)
                    else:
                        # Not pinned for this meeting - assign normally
                        slot_room_vars = [
                            X[section.crn, k, s.id, r.id]
                            for s in meeting.compatible_slots
                            for r in meeting.compatible_rooms
                            if (section.crn, k, s.id, r.id) in X
                        ]
                        if slot_room_vars:
                            model.Add(sum(slot_room_vars) == 1)

                # Faculty pinning
                if section.faculty_id:
                    if (section.crn, section.faculty_id) in Y:
                        model.Add(Y[section.crn, section.faculty_id] == 1)
                    for f in self.approved_faculty.get(course.code, []):
                        if (section.crn, f.id) in Y and f.id != section.faculty_id:
                            model.Add(Y[section.crn, f.id] == 0)
                continue  # Skip unpinned logic below

            # Unpinned: exactly one slot-room per meeting (or none if closed)
            for k, meeting in enumerate(meetings):
                slot_room_vars = [
                    X[section.crn, k, s.id, r.id]
                    for s in meeting.compatible_slots
                    for r in meeting.compatible_rooms
                    if (section.crn, k, s.id, r.id) in X
                ]
                if slot_room_vars:
                    if open_vars is not None:
                        model.Add(sum(slot_room_vars) == open_vars[section.crn])
                    else:
                        model.Add(sum(slot_room_vars) == 1)

            # Exactly one faculty assignment (or none if closed)
            faculty_vars = [
                Y[section.crn, f.id]
                for f in self.approved_faculty.get(course.code, [])
                if (section.crn, f.id) in Y
            ]
            if faculty_vars:
                if open_vars is not None:
                    model.Add(sum(faculty_vars) == open_vars[section.crn])
                else:
                    model.Add(sum(faculty_vars) == 1)

    def _crosslist_packs(
        self, sections: list[Section]
    ) -> dict[tuple[str, str], list[Section]]:
        """Cross-listed section packs: same group + same section number.

        Members of a pack are ONE physical class under several codes. The
        first (alphabetical) member is the pack leader; the rest mirror it.
        """
        packs: dict[tuple[str, str], list[Section]] = defaultdict(list)
        for s in sections:
            course = self.course_by_code.get(s.course_code)
            if course is not None and course.crosslist_group:
                packs[course.crosslist_group, s.section_number].append(s)
        return {
            key: sorted(pack, key=lambda s: s.course_code)
            for key, pack in packs.items()
            if len(pack) > 1
        }

    def _crosslist_secondary(self, sections: list[Section]) -> set[str]:
        """CRNs of non-leader pack members — they mirror their leader, so
        room/faculty occupancy constraints must count the leader only."""
        return {
            s.crn
            for pack in self._crosslist_packs(sections).values()
            for s in pack[1:]
        }

    def _add_crosslist_link_constraints(
        self, model: cp_model.CpModel, sections: list[Section],
        X: dict, Y: dict, Z: "dict | None" = None,
    ):
        """Pin cross-listed packs together: one class under several codes.

        - Placement: every meeting of a non-leader equals the leader's
          (options one side lacks are forced 0 — members must share slot and
          room types to be schedulable; the doctor warns on mismatches).
        - Instructor: identical Y assignment (an instructor must be approved
          for every member code, or the any-faculty fallback applies).
        - Joint capacity (full solve only, Z given): the combined enrollment
          across the pack respects the section cap and the one shared room.
        """
        packs = self._crosslist_packs(sections)
        if not packs:
            return

        x_by_meeting: dict[tuple[str, int], dict] = defaultdict(dict)
        for (crn, k, slot_id, room_id), var in X.items():
            x_by_meeting[crn, k][slot_id, room_id] = var

        z_by_crn: dict[str, list] = defaultdict(list)
        if Z:
            for (_sid, crn), var in Z.items():
                z_by_crn[crn].append(var)

        sec_cfg = self.config.section_settings
        float_capacity = sec_cfg.get("capacity_mode", "fixed") == "room"
        global_cap = sec_cfg.get("max_class_size")
        respect_rooms = self.config.hard_constraints.get("respect_room_capacity", True)

        open_vars = self._open_vars
        for (gid, _num), pack in packs.items():
            leader = pack[0]
            n_meetings = max(
                len(self.course_meetings.get(s.course_code, [])) for s in pack
            )
            if open_vars is not None:
                # A pack is one class: all codes open or close together, and
                # an open pack must be worth running on POOLED enrollment.
                for other in pack[1:]:
                    model.Add(open_vars[other.crn] == open_vars[leader.crn])
                if Z:
                    pooled = len({
                        d.student_id
                        for s in pack
                        for d in self.demands_by_course.get(s.course_code, [])
                    })
                    min_eff = min(
                        min(self.course_by_code[s.course_code].min_enrollment
                            for s in pack),
                        pooled,
                    )
                    pack_z = [v for s in pack for v in z_by_crn.get(s.crn, [])]
                    if min_eff > 0 and pack_z:
                        model.Add(sum(pack_z) >= min_eff * open_vars[leader.crn])
            for other in pack[1:]:
                for k in range(n_meetings):
                    lvars = x_by_meeting.get((leader.crn, k), {})
                    ovars = x_by_meeting.get((other.crn, k), {})
                    for key in set(lvars) | set(ovars):
                        lv, ov = lvars.get(key), ovars.get(key)
                        if lv is not None and ov is not None:
                            model.Add(lv == ov)
                        elif lv is not None:
                            model.Add(lv == 0)
                        else:
                            model.Add(ov == 0)
                l_fac = {f.id for f in self.approved_faculty.get(leader.course_code, [])}
                o_fac = {f.id for f in self.approved_faculty.get(other.course_code, [])}
                for fid in l_fac | o_fac:
                    lv = Y.get((leader.crn, fid))
                    ov = Y.get((other.crn, fid))
                    if lv is not None and ov is not None:
                        model.Add(lv == ov)
                    elif lv is not None:
                        model.Add(lv == 0)
                    elif ov is not None:
                        model.Add(ov == 0)

            if Z:
                joint_vars = [v for s in pack for v in z_by_crn.get(s.crn, [])]
                if not joint_vars:
                    continue
                joint = sum(joint_vars)
                if float_capacity:
                    ceilings = [
                        c for s in pack
                        if (c := (self.course_by_code[s.course_code].max_class_size
                                  or global_cap))
                    ]
                    if ceilings:
                        model.Add(joint <= min(ceilings))
                else:
                    model.Add(joint <= min(s.capacity for s in pack))
                if respect_rooms:
                    for k in range(n_meetings):
                        cap_terms = [
                            self.room_by_id[room_id].capacity * var
                            for (slot_id, room_id), var in
                            x_by_meeting.get((leader.crn, k), {}).items()
                            if room_id in self.room_by_id
                        ]
                        if cap_terms:
                            model.Add(joint <= sum(cap_terms))
        logger.info(f"Cross-list constraints: {len(packs)} section packs linked")

    def _add_room_constraints(
        self, model: cp_model.CpModel, sections: list[Section], X: dict
    ):
        """Room can't be double-booked.

        Uses pairwise overlap constraints: for each room and each DIRECTLY overlapping
        pair of slots (S1, S2), at most one section may be assigned to that room across
        both slots.  This is correct because the slot_overlaps graph is NOT a clique —
        e.g. slot 19 (Mon lab) and slot 25 (Wed lab) both overlap with slot 1 (MWF)
        but do NOT overlap with each other, so they CAN share a room.
        """
        if not self.config.hard_constraints.get("room_no_overlap", True):
            logger.info("room_no_overlap disabled in config")
            return

        # Index X vars by (room_id, slot_id). Cross-listed mirrors share the
        # leader's room deliberately — count only the leader, or the pack
        # would read as a double-booking of its own room.
        xl_secondary = self._crosslist_secondary(sections)
        x_by_room_slot: dict[tuple[int, int], list] = defaultdict(list)
        for (crn, k, slot_id, room_id), var in X.items():
            if crn in xl_secondary:
                continue
            x_by_room_slot[room_id, slot_id].append(var)

        if self._use_clique_no_overlap():
            # Time-point cliques (slotcliques.py): one constraint per
            # (room, clique) covers every overlapping pair AND the same-slot
            # case, with no spurious pairs, and is tighter for the LP
            # relaxation than the pairwise form below.
            covered = {slot_id for clique in self.slot_cliques for slot_id in clique}
            for room in self.rooms:
                for clique in self.slot_cliques:
                    vars_c = [
                        v for slot_id in clique
                        for v in x_by_room_slot.get((room.id, slot_id), ())
                    ]
                    if len(vars_c) > 1:
                        model.Add(sum(vars_c) <= 1)
                for slot in self.time_slots:
                    if slot.id in covered:
                        continue  # degenerate slot: still no double-booking
                    vars_s = x_by_room_slot.get((room.id, slot.id), [])
                    if len(vars_s) > 1:
                        model.Add(sum(vars_s) <= 1)
            return

        for room in self.rooms:
            # Same-slot: at most one section per room per slot
            for slot in self.time_slots:
                vars_s = x_by_room_slot.get((room.id, slot.id), [])
                if len(vars_s) > 1:
                    model.Add(sum(vars_s) <= 1)

            # Cross-slot: for each directly overlapping pair, combined sum <= 1
            processed: set[tuple[int, int]] = set()
            for slot in self.time_slots:
                vars_s1 = x_by_room_slot.get((room.id, slot.id), [])
                if not vars_s1:
                    continue
                for slot2_id in self.slot_overlaps.get(slot.id, set()):
                    key = (min(slot.id, slot2_id), max(slot.id, slot2_id))
                    if key in processed:
                        continue
                    processed.add(key)
                    vars_s2 = x_by_room_slot.get((room.id, slot2_id), [])
                    if not vars_s2:
                        continue
                    model.Add(sum(vars_s1) + sum(vars_s2) <= 1)

    def _add_faculty_constraints(
        self, model: cp_model.CpModel, sections: list[Section], X: dict, Y: dict,
        section_in_slot: dict,
    ):
        """Faculty can't be double-booked and must respect load limits.

        Uses pairwise overlap constraints (same fix as room constraints): the slot_overlaps
        graph is not a clique, so building indicators over the full group and requiring
        sum <= 1 was too restrictive (e.g. a faculty CAN teach in slot 19 Mon-lab and slot
        25 Wed-lab since those are different days and do not overlap).
        """
        no_overlap = self.config.hard_constraints.get("faculty_no_overlap", True)
        if not no_overlap:
            logger.info("faculty_no_overlap disabled in config")

        # Cross-listed mirrors share the leader's instructor and slot by
        # construction; counting them would double the occupancy.
        xl_secondary = self._crosslist_secondary(sections)

        for faculty in self.faculty:
            if no_overlap:
                # Build per-slot indicator lists: faculty_teaches_in_slot[slot_id]
                fsis: dict[int, list] = defaultdict(list)
                for section in sections:
                    if section.crn in xl_secondary:
                        continue
                    if (section.crn, faculty.id) not in Y:
                        continue
                    course = self.course_by_code[section.course_code]
                    meetings = self.course_meetings.get(course.code, [])
                    for k, meeting in enumerate(meetings):
                        for slot in meeting.compatible_slots:
                            if (section.crn, k, slot.id) not in section_in_slot:
                                continue
                            sis = section_in_slot[section.crn, k, slot.id]
                            y_var = Y[section.crn, faculty.id]
                            indicator = model.NewBoolVar(
                                f"fno_{section.crn}_{k}_{slot.id}_{faculty.id}"
                            )
                            # One direction is enough: the indicator only
                            # ever appears in "sum <= 1" constraints, so it
                            # must be forced UP when both hold and may stay
                            # 0 otherwise. The old upper bound doubled the
                            # constraint count for nothing.
                            model.Add(indicator >= sis + y_var - 1)
                            fsis[slot.id].append(indicator)

                if self._use_clique_no_overlap():
                    # Time-point cliques — same reasoning as the room version.
                    covered = {s for clique in self.slot_cliques for s in clique}
                    for clique in self.slot_cliques:
                        inds = [i for s in clique for i in fsis.get(s, ())]
                        if len(inds) > 1:
                            model.Add(sum(inds) <= 1)
                    for slot_id, indicators in fsis.items():
                        if slot_id not in covered and len(indicators) > 1:
                            model.Add(sum(indicators) <= 1)
                else:
                    # Same-slot: at most one section per slot for this faculty
                    for slot_id, indicators in fsis.items():
                        if len(indicators) > 1:
                            model.Add(sum(indicators) <= 1)

                    # Cross-slot pairwise: for each directly overlapping
                    # pair, combined sum <= 1
                    processed: set[tuple[int, int]] = set()
                    for slot_id, indicators_s1 in fsis.items():
                        for slot2_id in self.slot_overlaps.get(slot_id, set()):
                            key = (min(slot_id, slot2_id), max(slot_id, slot2_id))
                            if key in processed:
                                continue
                            processed.add(key)
                            indicators_s2 = fsis.get(slot2_id, [])
                            if not indicators_s2:
                                continue
                            model.Add(sum(indicators_s1) + sum(indicators_s2) <= 1)

            # Unavailable slots: faculty cannot teach at these times
            for slot_id in faculty.unavailable_slots:
                for section in sections:
                    if section.crn in xl_secondary:
                        continue  # mirrors the leader; leader carries the rule
                    if (section.crn, faculty.id) not in Y:
                        continue
                    course = self.course_by_code[section.course_code]
                    meetings = self.course_meetings.get(course.code, [])
                    for k, meeting in enumerate(meetings):
                        if (section.crn, k, slot_id) not in section_in_slot:
                            continue
                        # Unavailable: section can't be in this slot AND taught by this faculty
                        model.Add(
                            section_in_slot[section.crn, k, slot_id]
                            + Y[section.crn, faculty.id] <= 1
                        )

            # Faculty load limit (soft: only enforce if config requires it).
            # The ceiling is max_credits plus the person's approved overload
            # (or the blanket default_overload_credits).
            if self.config.hard_constraints.get("faculty_load_limit", False):
                credit_terms = []
                for section in sections:
                    if section.crn in xl_secondary:
                        continue  # one class taught once, not once per code
                    if (section.crn, faculty.id) in Y:
                        course = self.course_by_code[section.course_code]
                        credit_terms.append(Y[section.crn, faculty.id] * course.credits)
                if credit_terms:
                    model.Add(sum(credit_terms) <= self._effective_load_limit(faculty))

    def _add_linked_course_constraints(
        self, model: cp_model.CpModel, sections: list[Section], X: dict,
        section_in_slot: dict,
    ):
        """Linked courses (PARENT/CHILD) can't overlap across any meetings."""
        if not self.config.hard_constraints.get("linked_no_overlap", True):
            logger.info("linked_no_overlap disabled in config")
            return

        parent_sections = [
            s
            for s in sections
            if self.course_by_code[s.course_code].structure_type == StructureType.PARENT
        ]

        for parent in parent_sections:
            parent_course = self.course_by_code[parent.course_code]
            parent_meetings = self.course_meetings.get(parent_course.code, [])

            # Find child sections
            children = [
                s
                for s in sections
                if self.course_by_code[s.course_code].linked_course_code == parent_course.code
            ]

            for child in children:
                child_course = self.course_by_code[child.course_code]
                child_meetings = self.course_meetings.get(child_course.code, [])

                # Check all meeting combinations
                for pk, p_meeting in enumerate(parent_meetings):
                    for ck, c_meeting in enumerate(child_meetings):
                        for p_slot in p_meeting.compatible_slots:
                            if (parent.crn, pk, p_slot.id) not in section_in_slot:
                                continue
                            overlapping = self.slot_overlaps.get(p_slot.id, set())

                            for c_slot_id in overlapping:
                                if (child.crn, ck, c_slot_id) not in section_in_slot:
                                    continue
                                model.Add(
                                    section_in_slot[parent.crn, pk, p_slot.id]
                                    + section_in_slot[child.crn, ck, c_slot_id]
                                    <= 1
                                )

    def _add_capacity_constraints(
        self, model: cp_model.CpModel, sections: list[Section], X: dict, Z: dict
    ):
        """Enrollment must not exceed section capacity nor the assigned room's capacity.

        With respect_room_capacity on, each meeting adds
            sum(Z) <= sum(room.capacity * X[crn, k, slot, room])
        — exactly one X is 1 per meeting, so the right side collapses to the
        assigned room's capacity. Small rooms stay usable for low-enrollment
        sections; the solver just can't over-enroll past the room size.

        Capacity modes (section_settings["capacity_mode"]):
        - "fixed": enrollment <= section.capacity (the classic bound).
        - "room": class size floats — the section.capacity bound is dropped
          and enrollment is limited only by the assigned room plus any cap
          (the course's max_class_size, else the global max_class_size).
          _extract_solution then stores the room-derived size as the
          section's capacity.
        """
        respect_room_capacity = self.config.hard_constraints.get("respect_room_capacity", True)
        sec_cfg = self.config.section_settings
        float_capacity = sec_cfg.get("capacity_mode", "fixed") == "room"
        global_cap = sec_cfg.get("max_class_size")
        if float_capacity and not respect_room_capacity and not global_cap:
            logger.warning(
                "capacity_mode 'room' with respect_room_capacity off and no "
                "max_class_size cap — section enrollment is unbounded"
            )

        # Precompute Z vars grouped by CRN
        z_by_crn: dict[str, list] = defaultdict(list)
        for (student_id, crn), var in Z.items():
            z_by_crn[crn].append(var)

        # Index X vars with their room capacity by (crn, meeting); track the
        # largest compatible room per meeting for the static envelope below.
        x_caps_by_crn_meeting: dict[tuple[str, int], list] = defaultdict(list)
        max_cap_by_crn_meeting: dict[tuple[str, int], int] = {}
        if respect_room_capacity:
            for (crn, k, slot_id, room_id), var in X.items():
                room = self.room_by_id.get(room_id)
                if room is not None:
                    x_caps_by_crn_meeting[crn, k].append(room.capacity * var)
                    key = (crn, k)
                    if room.capacity > max_cap_by_crn_meeting.get(key, 0):
                        max_cap_by_crn_meeting[key] = room.capacity

        open_vars = self._open_vars
        for section in sections:
            open_var = open_vars[section.crn] if open_vars is not None else None
            course = self.course_by_code[section.course_code]
            student_vars = z_by_crn.get(section.crn, [])
            if not student_vars:
                # No one can enroll: with closure on, don't let an empty
                # section squat on a room. Cross-listed members are exempt
                # (one code of a pack may carry all the demand) — the pack's
                # joint bound governs them.
                if (
                    open_var is not None
                    and not section.is_pinned
                    and not course.crosslist_group
                ):
                    model.Add(open_var == 0)
                continue
            enrollment = sum(student_vars)

            # Open sections must be worth running: at least min_enrollment
            # students, or ALL of the course's demand when demand is below
            # the minimum (nobody gets stranded to satisfy a headcount).
            # Cross-listed members get a pack-level bound instead.
            if open_var is not None and not course.crosslist_group:
                demand_n = len(self.demands_by_course.get(course.code, []))
                min_eff = min(course.min_enrollment, demand_n)
                if min_eff > 0:
                    model.Add(enrollment >= min_eff * open_var)

            if float_capacity:
                ceiling = course.max_class_size or global_cap
                if ceiling:
                    if open_var is not None:
                        model.Add(enrollment <= ceiling * open_var)
                    else:
                        model.Add(enrollment <= ceiling)
                elif respect_room_capacity:
                    # Static envelope: with no cap configured, enrollment
                    # still can never exceed the largest compatible room at
                    # any meeting. Redundant with the per-meeting dynamic
                    # bound below, but a fixed bound the search can use —
                    # without it the relaxation assumes unbounded sections
                    # and struggles to find a first feasible solution.
                    meetings = self.course_meetings.get(course.code, [])
                    static_caps = [
                        max_cap_by_crn_meeting[section.crn, k]
                        for k in range(len(meetings))
                        if (section.crn, k) in max_cap_by_crn_meeting
                    ]
                    if static_caps:
                        if open_var is not None:
                            model.Add(enrollment <= min(static_caps) * open_var)
                        else:
                            model.Add(enrollment <= min(static_caps))
            else:
                if open_var is not None:
                    model.Add(enrollment <= section.capacity * open_var)
                else:
                    model.Add(enrollment <= section.capacity)

            if respect_room_capacity:
                meetings = self.course_meetings.get(course.code, [])
                for k in range(len(meetings)):
                    cap_terms = x_caps_by_crn_meeting.get((section.crn, k), [])
                    if cap_terms:
                        model.Add(enrollment <= sum(cap_terms))

    def _add_intra_section_constraints(
        self, model: cp_model.CpModel, sections: list[Section], X: dict,
        section_in_slot: dict,
    ):
        """Different meetings of the same section can't overlap."""
        for section in sections:
            course = self.course_by_code[section.course_code]
            meetings = self.course_meetings.get(course.code, [])

            if len(meetings) <= 1:
                continue  # Single-meeting, no intra-section overlap possible

            # For each pair of meetings within this section
            for (k1, m1), (k2, m2) in combinations(enumerate(meetings), 2):
                for slot1 in m1.compatible_slots:
                    if (section.crn, k1, slot1.id) not in section_in_slot:
                        continue
                    overlapping = self.slot_overlaps.get(slot1.id, set()) | {slot1.id}

                    for slot2_id in overlapping:
                        if (section.crn, k2, slot2_id) not in section_in_slot:
                            continue
                        model.Add(
                            section_in_slot[section.crn, k1, slot1.id]
                            + section_in_slot[section.crn, k2, slot2_id]
                            <= 1
                        )

    def _add_graduation_critical_constraints(
        self, model: cp_model.CpModel, sections: list[Section], Z: dict
    ):
        """Graduation-critical demands must be satisfied (hard constraint).

        For each demand where is_graduation_critical=True, the student
        must be enrolled in at least one section of that course.
        """
        if not self.config.hard_constraints.get("graduation_critical_required", True):
            logger.info("Graduation-critical constraints disabled in config")
            return

        critical_demands = [d for d in self.demands if d.is_graduation_critical]

        if not critical_demands:
            logger.info("No graduation-critical demands to constrain")
            return

        constraints_added = 0
        for demand in critical_demands:
            # Find all sections of this course
            course_sections = [s for s in sections if s.course_code == demand.course_code]

            # Get Z variables for this student and these sections
            z_vars = [
                Z[demand.student_id, section.crn]
                for section in course_sections
                if (demand.student_id, section.crn) in Z
            ]

            if z_vars:
                # Student must be enrolled in at least one section
                model.Add(sum(z_vars) >= 1)
                constraints_added += 1

        logger.info(
            f"Added {constraints_added} graduation-critical constraints "
            f"(from {len(critical_demands)} critical demands)"
        )

    def _add_single_enrollment_constraints(
        self, model: cp_model.CpModel, sections: list[Section], Z: dict
    ):
        """A student takes at most ONE section of any given course.

        Without this, every extra seat is free objective reward: whenever a
        course has more capacity than demand, the solver double-enrolls the
        same students into sibling sections to double-count their urgency
        (caught in the Fall'26 back-test rehearsal — +592 phantom
        enrollments once room-mode capacity made seats plentiful).
        """
        sections_by_course: dict[str, list[Section]] = defaultdict(list)
        for s in sections:
            sections_by_course[s.course_code].append(s)

        constraints_added = 0
        for demand in self.demands:
            course_sections = sections_by_course.get(demand.course_code, [])
            if len(course_sections) < 2:
                continue
            z_vars = [
                Z[demand.student_id, s.crn]
                for s in course_sections
                if (demand.student_id, s.crn) in Z
            ]
            if len(z_vars) > 1:
                model.Add(sum(z_vars) <= 1)
                constraints_added += 1
        logger.info(f"Added {constraints_added} single-enrollment constraints")

    def _compute_section_in_slot(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        X: dict,
    ) -> dict[tuple[str, int, int], Any]:
        """Compute indicator variables: is meeting k of section in slot s?

        Returns dict[(crn, k, slot_id) -> BoolVar] where value is 1 iff
        meeting k of section is assigned to that slot (any room).
        """
        section_in_slot: dict[tuple[str, int, int], Any] = {}

        for section in sections:
            course = self.course_by_code[section.course_code]
            meetings = self.course_meetings.get(course.code, [])

            for k, meeting in enumerate(meetings):
                for slot in meeting.compatible_slots:
                    room_vars = [
                        X[section.crn, k, slot.id, room.id]
                        for room in meeting.compatible_rooms
                        if (section.crn, k, slot.id, room.id) in X
                    ]
                    if not room_vars:
                        continue

                    if len(room_vars) == 1:
                        section_in_slot[section.crn, k, slot.id] = room_vars[0]
                    else:
                        indicator = model.NewBoolVar(
                            f"sis_{section.crn}_{k}_{slot.id}"
                        )
                        model.AddMaxEquality(indicator, room_vars)
                        section_in_slot[section.crn, k, slot.id] = indicator

        return section_in_slot

    def _add_faculty_travel_time_constraints(
        self,
        model: cp_model.CpModel,
        sections: list[Section],
        X: dict,
        Y: dict,
    ):
        """Hard constraint: forbid faculty back-to-back in different buildings when gap is too
        short.
        """
        if not self.config.hard_constraints.get("min_travel_time", 10):
            return

        if not any(self.slot_tight_adjacencies.values()):
            logger.info("No tight-adjacent slot pairs — skipping faculty travel time constraints")
            return

        for faculty in self.faculty:
            # Index this faculty's possible (crn, k, room_id) by slot, so we
            # only pair assignments across tight-adjacent slot pairs. The old
            # all-pairs loop was O(n²) over every (crn, k, slot, room) — a
            # non-starter once tight adjacencies exist at real scale.
            by_slot: dict[int, list[tuple[str, int, str]]] = defaultdict(list)
            for section in sections:
                if (section.crn, faculty.id) not in Y:
                    continue
                course = self.course_by_code[section.course_code]
                meetings = self.course_meetings.get(course.code, [])
                for k, meeting in enumerate(meetings):
                    for slot in meeting.compatible_slots:
                        for room in meeting.compatible_rooms:
                            if (section.crn, k, slot.id, room.id) in X:
                                by_slot[slot.id].append((section.crn, k, room.id))

            seen_pairs: set[tuple[int, int]] = set()
            for s1, entries1 in by_slot.items():
                for s2 in self.slot_tight_adjacencies.get(s1, set()):
                    pair_key = (min(s1, s2), max(s1, s2))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    entries2 = by_slot.get(s2, [])
                    for crn1, k1, r1 in entries1:
                        room1 = self.room_by_id.get(r1)
                        if not room1:
                            continue
                        for crn2, k2, r2 in entries2:
                            if crn1 == crn2 and k1 == k2:
                                continue
                            room2 = self.room_by_id.get(r2)
                            if not room2 or room1.building == room2.building:
                                continue
                            # Forbid: both assigned AND both taught by this faculty
                            model.Add(
                                X[crn1, k1, s1, r1]
                                + Y[crn1, faculty.id]
                                + X[crn2, k2, s2, r2]
                                + Y[crn2, faculty.id]
                                <= 3
                            )

    # ── Formulation switches & symmetry breaking ─────────────────────────

    def _use_clique_no_overlap(self) -> bool:
        """solver.no_overlap_formulation: "cliques" (default) or "pairwise"."""
        return (
            self.config.solver_settings.get("no_overlap_formulation", "cliques")
            == "cliques"
        )

    def _sibling_groups(self, sections: list[Section]) -> list[list[Section]]:
        """Interchangeable sibling sections, each group in canonical order.

        Sections of one course with the same capacity and honors flag see
        identical constraints and objective terms, so the solver cannot
        tell them apart and any solution stays valid under a permutation of
        them. Pinned sections, cross-listed packs and repair mode (anchors
        are per CRN) are excluded.
        """
        if self._repair_anchor:
            return []
        groups: dict[tuple, list[Section]] = defaultdict(list)
        for s in sections:
            course = self.course_by_code.get(s.course_code)
            if s.is_pinned or course is None or course.crosslist_group:
                continue
            groups[s.course_code, s.capacity, s.is_honors].append(s)
        return [
            sorted(group, key=lambda s: (s.section_number, s.crn))
            for group in groups.values()
            if len(group) > 1
        ]

    def _slot_order_index(self) -> dict[int, int]:
        """A fixed 1-based total order of slot ids (any order works, as
        long as hints and constraints agree on it)."""
        return {
            s.id: i + 1
            for i, s in enumerate(sorted(self.time_slots, key=lambda s: s.id))
        }

    def _add_sibling_symmetry_breaking(
        self, model: cp_model.CpModel, sections: list[Section], section_in_slot: dict
    ) -> int:
        """Order interchangeable siblings by their first meeting's slot.

        Without this, N sibling sections admit N! relabelings of every
        solution: the search re-proves the same bound N! times and the
        reported optimality gap barely moves. With it, sibling i's meeting-0
        slot index is <= sibling i+1's (ties allowed — same slot, different
        rooms). With section closure on, open siblings come first and the
        slot order only binds between open ones. Warm-start hints are
        canonicalized to the same order (warmstart._canonicalize_sibling_hints),
        otherwise they would violate these constraints and be wasted.
        Disable with solver.symmetry_breaking: false. Returns the number of
        groups ordered.
        """
        if not self.config.solver_settings.get("symmetry_breaking", True):
            return 0
        slot_index = self._slot_order_index()
        big = len(slot_index) + 1
        sis0: dict[str, list] = defaultdict(list)
        for (crn, k, slot_id), var in section_in_slot.items():
            if k == 0 and slot_id in slot_index:
                sis0[crn].append(slot_index[slot_id] * var)
        open_vars = self._open_vars
        ordered = 0
        for group in self._sibling_groups(sections):
            exprs = [sum(sis0[s.crn]) if sis0.get(s.crn) else None for s in group]
            for a, b, ea, eb in zip(group, group[1:], exprs, exprs[1:]):
                if ea is None or eb is None:
                    continue
                if open_vars is None:
                    model.Add(ea <= eb)
                else:
                    oa, ob = open_vars[a.crn], open_vars[b.crn]
                    model.Add(oa >= ob)
                    model.Add(ea <= eb + big * (1 - ob))
            ordered += 1
        logger.info("Symmetry breaking: %d sibling groups ordered", ordered)
        return ordered
