"""Pre-solve staffing feasibility checks.

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

from src.database.models import (
    Faculty,
    Section,
)

logger = logging.getLogger(__name__)


class StaffingCheckMixin:
    """Pre-solve staffing feasibility checks."""

    def _effective_load_limit(self, faculty: Faculty) -> int:
        """max_credits plus the person's approved overload (or the blanket)."""
        overload = faculty.max_overload_credits
        if overload is None:
            overload = int(
                self.config.hard_constraints.get("default_overload_credits", 0)
            )
        return faculty.max_credits + max(0, overload)

    def _staffing_preflight(self, sections: list[Section]) -> list[str]:
        """Detect approved-faculty groups that cannot possibly staff their sections.

        Every section whose course has approved faculty MUST be staffed by one
        of them (_add_assignment_constraints), and faculty_no_overlap means one
        person teaches at most one section per time slot. So for any connected
        group of courses sharing approved faculty, the sections needed can never
        exceed the distinct usable slots summed over the group's instructors.
        Violations are structural infeasibility — CP-SAT typically burns its
        whole budget failing to prove it and returns "unknown" with no clue.

        The slot count per instructor is optimistic (slot overlaps ignored), so
        every warning returned here is a certain impossibility, not a maybe.
        Courses with no specifically qualified instructors (any-faculty fallback) are excluded;
        their sections can borrow anyone, which only makes this check more
        conservative.
        """
        count_by_course: dict[str, int] = defaultdict(int)
        xl_secondary = self._crosslist_secondary(sections)
        for s in sections:
            if s.crn in xl_secondary:
                continue  # cross-listed mirror: taught once with its leader
            count_by_course[s.course_code] += 1

        warnings: list[str] = []

        # FAIL LOUDLY first: a course NOBODY can teach (no one lists it in
        # approved_courses and there are no unrestricted instructors) would
        # otherwise be scheduled silently with no instructor at all.
        for code in sorted(count_by_course):
            if not self.approved_faculty.get(code):
                warnings.append(
                    f"UNSTAFFABLE: {code} has no qualified instructor — no one "
                    f"lists it in approved_courses and there are no "
                    f"unrestricted instructors — so its "
                    f"{count_by_course[code]} section(s) would run with NO "
                    f"instructor assigned. Qualify someone to teach it (add it "
                    f"to their approved_courses, possibly as an overload) "
                    f"or plan a hire."
                )

        # Instructors with an explicit approved_courses list are "dedicated";
        # ones with an empty list can teach anything (the any-faculty
        # fallback) and form a shared pool every course may borrow from.
        dedicated_ids = {f.id for f in self.faculty if f.approved_courses}
        restricted = {
            code
            for code in count_by_course
            if any(f.id in dedicated_ids for f in self.approved_faculty.get(code, []))
        }
        if not restricted:
            return self._finalize_staffing_warnings(warnings)

        # Union-find: courses connected through a shared DEDICATED instructor
        # (pool instructors don't merge groups — they're accounted separately).
        parent = {code: code for code in restricted}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        fac_courses: dict[str, list[str]] = defaultdict(list)
        for code in restricted:
            for f in self.approved_faculty[code]:
                if f.id in dedicated_ids:
                    fac_courses[f.id].append(code)
        for codes in fac_courses.values():
            root = find(codes[0])
            for other in codes[1:]:
                parent[find(other)] = root

        groups: dict[str, list[str]] = defaultdict(list)
        for code in restricted:
            groups[find(code)].append(code)

        def usable_slots(fac, codes) -> int:
            slot_ids: set[int] = set()
            for c in codes:
                if any(f.id == fac.id for f in self.approved_faculty.get(c, [])):
                    for meeting in self.course_meetings.get(c, []):
                        slot_ids.update(sl.id for sl in meeting.compatible_slots)
            slot_ids -= set(fac.unavailable_slots)
            return len(slot_ids)

        pool = [f for f in self.faculty if f.id not in dedicated_ids]
        pool_capacity = sum(usable_slots(f, list(restricted)) for f in pool)

        load_limit_on = self.config.hard_constraints.get("faculty_load_limit", False)
        deficits: list[tuple[list[str], int]] = []  # (courses, unstaffable count)
        for codes in groups.values():
            need = sum(
                count_by_course[c]
                * max(1, len(self.course_meetings.get(c, [])))
                for c in codes
            )
            own = sorted(
                {f.id for c in codes for f in self.approved_faculty[c]}
                & dedicated_ids
            )
            fac_by_id = {f.id: f for f in self.faculty}
            own_capacity = sum(usable_slots(fac_by_id[fid], codes) for fid in own)
            if need > own_capacity + pool_capacity:
                warnings.append(
                    f"Staffing bottleneck: {', '.join(sorted(codes))} need {need} "
                    f"section-meetings, but their qualified instructor(s) "
                    f"({', '.join(own)}) plus the {len(pool)} unrestricted "
                    f"instructor(s) can cover at most {own_capacity + pool_capacity} "
                    f"teaching slots — this schedule is impossible to staff. "
                    f"Qualify more instructors, reduce sections "
                    f"(students_per_section / max_sections_per_course), or widen "
                    f"valid_slot_types."
                )
            elif need > own_capacity:
                deficits.append((sorted(codes), need - own_capacity))

            # Credits ceiling (only when load limits are enforced): states
            # the exact overload shortfall so admin knows what to approve.
            if load_limit_on:
                needed_credits = sum(
                    count_by_course[c] * self.course_by_code[c].credits
                    for c in codes
                )
                credit_cap = (
                    sum(self._effective_load_limit(fac_by_id[fid]) for fid in own)
                    + sum(self._effective_load_limit(f) for f in pool)
                )
                if needed_credits > credit_cap:
                    warnings.append(
                        f"Overload shortfall: {', '.join(sorted(codes))} need "
                        f"{needed_credits} teaching credits, but their "
                        f"instructors' load limits (including approved "
                        f"overloads) total {credit_cap} — approve at least "
                        f"{needed_credits - credit_cap} more overload "
                        f"credit(s) (max_overload_credits in faculty.csv, or "
                        f"default_overload_credits in config), or add staff."
                    )

        # The pool is SHARED: even if each group fits with borrowing, the sum
        # of all borrowing must fit in the pool.
        total_deficit = sum(d for _, d in deficits)
        if total_deficit > pool_capacity:
            worst = sorted(deficits, key=lambda t: -t[1])[:4]
            detail = "; ".join(
                f"{'/'.join(codes[:3])}{'…' if len(codes) > 3 else ''} "
                f"short {d}" for codes, d in worst
            )
            warnings.append(
                f"Staffing bottleneck: course groups need {total_deficit} "
                f"section-meetings beyond what their own qualified instructors "
                f"can teach, but the shared unrestricted-instructor pool "
                f"({len(pool)} people) covers at most {pool_capacity}. "
                f"Impossible to staff everything ({detail}). Qualify more "
                f"instructors or reduce sections."
            )

        return self._finalize_staffing_warnings(warnings)

    def _finalize_staffing_warnings(self, warnings: list[str]) -> list[str]:
        """Shared tail: closure caveat + logging for every pre-flight exit."""
        if warnings and self.config.section_settings.get(
            "allow_section_closure", False
        ):
            warnings = [
                w + " (Section closure is enabled: the solver may close "
                    "sections instead of failing — expect unmet demand rather "
                    "than an unsolvable model.)"
                for w in warnings
            ]
        for w in warnings:
            logger.warning(w)
        return warnings
