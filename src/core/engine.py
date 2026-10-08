"""OR-Tools CP-SAT solver engine for Gradimize.

This module owns ``SchedulingEngine`` and its orchestration: loading a term's
data, generating sections, running the solve (single- or two-phase, plus the
re-solve and repair variants), diagnosing infeasibility, and extracting the
solution. The model-construction bodies it used to hold were extracted into
four mixin modules it composes, so each concern lives in its own readable file:

    model_build.ModelBuildMixin    variables + hard constraints
    penalties.PenaltyMixin         soft penalties + objective assembly
    warmstart.WarmStartMixin       warm-start hints + two-phase placement
    staffing_check.StaffingCheckMixin  pre-solve staffing feasibility

Those mixins run as engine methods (``self`` is the full engine), so they read
the data indexes built in ``_load_data`` and the per-solve state set in
``solve``. The config and result value types live in ``scheduler_config.py``.
All of those names are re-exported here, so existing
``from src.core.engine import SchedulerConfig`` call sites keep working.
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from ortools.sat.python import cp_model
from sqlmodel import Session, select

from src.core.cadence import is_off_cadence
from src.core.conflicts import ConflictMatrix, TimeSlotOverlapCalculator
from src.core.model_build import ModelBuildMixin
from src.core.penalties import PenaltyMixin
from src.core.scheduler_config import (  # noqa: F401 — re-exported for callers
    INFEASIBILITY_HINTS,
    MeetingInfo,
    SchedulerConfig,
    SolverResult,
    compute_blocked_slot_ids,
)
from src.core.sectioning import SectionSpec, count_student_overlaps, section_students
from src.core.slotcliques import compute_slot_cliques
from src.core.solver_util import resolve_worker_count
from src.core.staffing_check import StaffingCheckMixin
from src.core.timeutil import parse_semester
from src.core.warmstart import WarmStartMixin
from src.database.models import (
    Course,
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    StudentDemand,
    TimeSlot,
)

logger = logging.getLogger(__name__)


class _AssumptionProxy:
    """Wraps CpModel so every Add() between family() calls is enforced only
    if that family's assumption literal is true. Lets an infeasibility
    diagnosis re-run the SAME constraint methods and ask CP-SAT which
    families jointly conflict. Structural constraints created through other
    methods (AddMaxEquality indicator definitions, etc.) stay unconditional —
    they define variable semantics and cannot conflict on their own."""

    def __init__(self, model: cp_model.CpModel):
        self.model = model
        self.families: dict[str, Any] = {}
        self._current = None

    def family(self, name: str) -> None:
        lit = self.model.NewBoolVar(f"assume::{name}")
        self.families[name] = lit
        self._current = lit

    def Add(self, ct):
        constraint = self.model.Add(ct)
        if self._current is not None:
            constraint.OnlyEnforceIf(self._current)
        return constraint

    def __getattr__(self, name):
        return getattr(self.model, name)


class SchedulingEngine(
    ModelBuildMixin, PenaltyMixin, WarmStartMixin, StaffingCheckMixin
):
    """CP-SAT based course scheduling engine."""

    def __init__(
        self,
        session: Session,
        conflict_matrix: ConflictMatrix,
        config: SchedulerConfig | None = None,
    ):
        """Initialize scheduling engine.

        Args:
            session: Database session.
            conflict_matrix: Course conflict matrix.
            config: Scheduler configuration.
        """
        self.session = session
        self.conflict_matrix = conflict_matrix
        self.config = config or SchedulerConfig()

        # Precompute slot overlaps
        self.overlap_calc = TimeSlotOverlapCalculator(session)
        self.slot_overlaps = self.overlap_calc.compute_overlaps()

        # Precompute slot adjacencies for back-to-back and travel time
        self.slot_adjacencies = self.overlap_calc.compute_adjacencies(max_gap_minutes=15)
        min_travel = self.config.hard_constraints.get("min_travel_time", 10)
        self.slot_tight_adjacencies = self.overlap_calc.compute_tight_adjacencies(min_travel)

        # Pinned meeting data for re-solve (CRN -> list of (slot_id, room_id) by meeting_seq)
        self._pinned_meetings: dict[str, list[tuple[int, str]]] = {}

        # Repair-mode anchor (repair_solve): the published schedule to stay
        # close to. CRN -> {"meetings": [(slot_id, room_id), ...],
        # "faculty_id": str|None, "students": [student_id, ...]}. When set,
        # solve() adds stability rewards and hints the base verbatim.
        self._repair_anchor: dict[str, dict] | None = None

        # Section-closure BoolVars for the CURRENT model (CRN -> open var), or
        # None when closure is off. solve() sets it per run; the two-phase and
        # infeasibility-diagnosis paths save/None/restore it because they build
        # a separate model whose builders must not see the prior model's vars.
        self._open_vars: dict[str, Any] | None = None

        # What the exact student-sectioning pass (phase B) did on the last
        # solve, for result.stats["sectioning"]; None if it did not run.
        self._last_sectioning: dict[str, Any] | None = None

        # Load data
        self._load_data()

    def _load_data(self):
        """Load all required data from database."""
        self.time_slots = list(self.session.exec(select(TimeSlot)).all())
        self.rooms = list(self.session.exec(select(Room).where(Room.is_active == True)).all())  # noqa: E712
        self.courses = list(self.session.exec(select(Course)).all())
        self.faculty = list(self.session.exec(select(Faculty)).all())
        self.demands = list(self.session.exec(select(StudentDemand)).all())

        # Compute blocked slots from config
        self.blocked_slot_ids = compute_blocked_slot_ids(
            self.time_slots, self.config.blocked_periods
        )
        if self.blocked_slot_ids:
            blocked_slots = [s for s in self.time_slots if s.id in self.blocked_slot_ids]
            blocked_names = [s.name for s in blocked_slots]
            logger.info(
                f"Blocked {len(self.blocked_slot_ids)} slots due to blocked periods: "
                f"{blocked_names}"
            )

        # Index data for quick lookup
        self.slot_by_id = {s.id: s for s in self.time_slots}
        # Time-point cliques for no-overlap constraints and phase B (slotcliques.py)
        self.slot_cliques = compute_slot_cliques(self.time_slots)
        self.room_by_id = {r.id: r for r in self.rooms}
        self.course_by_code = {c.code: c for c in self.courses}

        # Group demands by student and course
        self.demands_by_student: dict[str, list[StudentDemand]] = defaultdict(list)
        self.demands_by_course: dict[str, list[StudentDemand]] = defaultdict(list)
        for d in self.demands:
            self.demands_by_student[d.student_id].append(d)
            self.demands_by_course[d.course_code].append(d)

        # Compute compatible slots/rooms for each course (course-level, kept for backward compat)
        self._compute_compatibility()

        # Compute approved faculty for each course
        self._compute_faculty_approval()

        logger.info(
            f"Loaded: {len(self.time_slots)} slots, {len(self.rooms)} rooms, "
            f"{len(self.courses)} courses, {len(self.faculty)} faculty, "
            f"{len(self.demands)} demands"
        )

    # Room type fallbacks: when a primary room type is fully booked, these types are also
    # acceptable. This prevents infeasibility when there are more sections requiring a room
    # type than slots available.
    ROOM_TYPE_FALLBACKS: dict[str, list[str]] = {
        # Auditorium sections can use lecture halls (capacity still enforced)
        "AUDITORIUM": ["LECTURE"],
        "SEMINAR": ["LECTURE"],     # Seminars can use lecture halls
    }

    def _expand_room_types(self, required_types: set[str]) -> set[str]:
        """Expand room types with fallback types to avoid infeasibility from room type
        bottlenecks.
        """
        expanded = set(required_types)
        for rt in required_types:
            expanded.update(self.ROOM_TYPE_FALLBACKS.get(rt, []))
        return expanded

    def _compute_compatibility(self):
        """Compute compatible slots and rooms for each course, including per-meeting."""
        self.compatible_slots: dict[str, list[TimeSlot]] = {}
        self.compatible_rooms: dict[str, list[Room]] = {}
        self.course_meetings: dict[str, list[MeetingInfo]] = {}

        match_room_type = self.config.hard_constraints.get("match_room_type", True)

        for course in self.courses:
            # Course-level compatibility (used for single-meeting courses and backward compat)
            valid_types = set(course.valid_slot_types)
            self.compatible_slots[course.code] = [
                s for s in self.time_slots
                if s.slot_type in valid_types and s.id not in self.blocked_slot_ids
            ]

            required_types = set(course.required_room_types)
            if match_room_type:
                allowed_types = self._expand_room_types(required_types)
                self.compatible_rooms[course.code] = [
                    r for r in self.rooms if r.room_type.value in allowed_types
                ]
            else:
                self.compatible_rooms[course.code] = list(self.rooms)

            # Build per-meeting info
            if course.meeting_patterns:
                meetings = []
                for pattern in course.meeting_patterns:
                    slot_types = set(pattern.get("slot_types", []))
                    room_types = set(pattern.get("room_types", []))
                    m_slots = [
                        s for s in self.time_slots
                        if s.slot_type in slot_types and s.id not in self.blocked_slot_ids
                    ]
                    if match_room_type:
                        allowed_types = self._expand_room_types(room_types)
                        m_rooms = [r for r in self.rooms if r.room_type.value in allowed_types]
                    else:
                        m_rooms = list(self.rooms)
                    meetings.append(MeetingInfo(compatible_slots=m_slots, compatible_rooms=m_rooms))
                self.course_meetings[course.code] = meetings
            else:
                # Single meeting using course-level compatibility
                self.course_meetings[course.code] = [
                    MeetingInfo(
                        compatible_slots=self.compatible_slots[course.code],
                        compatible_rooms=self.compatible_rooms[course.code],
                    )
                ]

    def _compute_faculty_approval(self):
        """Compute which faculty can teach each course."""
        self.approved_faculty: dict[str, list[Faculty]] = {}

        require_approval = self.config.hard_constraints.get("faculty_approval_required", True)

        for course in self.courses:
            if not require_approval:
                self.approved_faculty[course.code] = list(self.faculty)
                continue
            approved = []
            for f in self.faculty:
                # Empty approved_courses means can teach anything
                if not f.approved_courses or course.code in f.approved_courses:
                    approved.append(f)
            self.approved_faculty[course.code] = approved

    def create_sections(
        self, semester: str, sections_per_course: dict[str, int] | None = None
    ) -> list[Section]:
        """Create section objects based on demand.

        Off-cadence courses (Course.offer_terms doesn't include this term, or
        Course.cadence rules out this year) need demand >= the configured
        threshold to spawn a section; otherwise they're skipped. Explicit
        overrides via sections_per_course bypass the gate.

        Args:
            semester: Semester code (e.g., "2025SP").
            sections_per_course: Override for number of sections per course.

        Returns:
            List of Section objects (not yet scheduled).
        """
        sections = []
        sec_cfg = self.config.section_settings
        crn_counter = sec_cfg["crn_start"]
        per_section = sec_cfg["students_per_section"]
        max_sections = sec_cfg["max_sections_per_course"]
        threshold = self.config.off_cadence_demand_threshold
        try:
            year, term_name = parse_semester(semester)
        except ValueError:
            logger.warning(
                "Could not parse semester '%s'; off-cadence gating disabled", semester
            )
            year, term_name = None, None

        skipped_off_cadence: list[tuple[str, int]] = []

        # Cross-listed groups are ONE physical class under several codes:
        # size them by POOLED demand (unique students across member codes),
        # and give every member the same section count so solve() can pair
        # section i of each member together.
        group_pooled: dict[str, int] = {}
        for course in self.courses:
            gid = course.crosslist_group
            if gid and gid not in group_pooled:
                members = [c for c in self.courses if c.crosslist_group == gid]
                group_pooled[gid] = len({
                    d.student_id
                    for m in members
                    for d in self.demands_by_course.get(m.code, [])
                })

        for course in self.courses:
            demand_count = len(self.demands_by_course.get(course.code, []))
            if course.crosslist_group:
                demand_count = group_pooled[course.crosslist_group]
            override = sections_per_course.get(course.code) if sections_per_course else None

            if override is None and year is not None:
                if is_off_cadence(course, year, term_name):
                    if demand_count < threshold:
                        skipped_off_cadence.append((course.code, demand_count))
                        continue
                    # Off-cadence but demand crossed the threshold — the
                    # cadence is being OVERRIDDEN. Say so loudly: the
                    # department set that cadence on purpose.
                    num_sections = max(1, -(-demand_count // per_section))
                    logger.warning(
                        "OFF-CADENCE OVERRIDE: %s is not normally offered in "
                        "%s (cadence %s%s), but %d students need it "
                        "(>= threshold %d) — offering %d section(s) anyway. "
                        "Raise off_cadence_demand_threshold to enforce the "
                        "cadence strictly.",
                        course.code, semester, course.cadence,
                        f", terms {course.offer_terms}" if course.offer_terms else "",
                        demand_count, threshold, min(num_sections, max_sections),
                    )
                else:
                    num_sections = max(1, -(-demand_count // per_section))
            elif override is not None:
                num_sections = override
            else:
                num_sections = max(1, -(-demand_count // per_section))

            num_sections = min(num_sections, max_sections)

            for i in range(num_sections):
                section = Section(
                    schedule_id=0,  # Will be set when schedule is created
                    crn=str(crn_counter),
                    course_code=course.code,
                    section_number=f"{i + 1:03d}",
                    # Per-course cap wins over the global default. In "room"
                    # capacity mode this is provisional — _extract_solution
                    # rewrites it from the assigned room.
                    capacity=course.max_class_size or sec_cfg["default_capacity"],
                    is_honors=False,
                    is_pinned=False,
                )
                sections.append(section)
                crn_counter += 1

        if skipped_off_cadence:
            logger.info(
                "Skipped %d off-cadence courses below threshold %d: %s",
                len(skipped_off_cadence),
                threshold,
                ", ".join(f"{c}({d})" for c, d in skipped_off_cadence[:10])
                + (" …" if len(skipped_off_cadence) > 10 else ""),
            )
        logger.info(f"Created {len(sections)} sections for {len(self.courses)} courses")
        return sections

    def solve(
        self,
        sections: list[Section],
        schedule_name: str = "Generated Schedule",
        semester: str = "2025SP",
        log_callback: "Callable[[str], None] | None" = None,
        viz_emit: "Callable[..., None] | None" = None,
        viz: "bool | None" = None,
    ) -> SolverResult:
        """Solve the scheduling problem.

        Args:
            sections: Sections to schedule.
            schedule_name: Name for the schedule version.
            semester: Semester code.
            log_callback: Optional callback receiving solver log lines.

        Returns:
            SolverResult with scheduled sections.
        """
        result = SolverResult()
        start_time = datetime.now()

        # Create CP model
        model = cp_model.CpModel()

        build_start = datetime.now()

        def _timed(label, fn, *args):
            import time
            if log_callback:
                log_callback(f"  [dim]{label}...[/dim]")
                time.sleep(0.01)  # yield GIL so TUI can render
            t0 = datetime.now()
            result = fn(*args)
            elapsed = (datetime.now() - t0).total_seconds()
            msg = f"  {label}: {elapsed:.2f}s"
            logger.info(msg)
            if log_callback:
                log_callback(msg)
            return result

        # Cheap structural check BEFORE the (possibly minutes-long) solve:
        # an over-committed approved-faculty group makes the model infeasible
        # in a way CP-SAT can rarely prove quickly — it just burns the whole
        # time budget and returns "unknown". Name the bottleneck up front.
        staffing_warnings = _timed(
            "staffing_preflight", self._staffing_preflight, sections
        )
        result.staffing_warnings = staffing_warnings
        if staffing_warnings and log_callback:
            for w in staffing_warnings:
                log_callback(f"[yellow]WARNING: {w}[/yellow]")

        # Create decision variables
        X, Y, Z = _timed("create_variables", self._create_variables, model, sections)

        # Optional section closure: O[crn] = 1 iff the section runs. Closed
        # sections get no placement, no instructor, no students — their room
        # and instructor go back into the pool.
        closure_on = self.config.section_settings.get("allow_section_closure", False)
        self._open_vars = (
            {s.crn: model.NewBoolVar(f"open_{s.crn}") for s in sections}
            if closure_on else None
        )

        # Precompute slot-level indicators (used by multiple constraint methods)
        section_in_slot = _timed(
            "compute_section_in_slot", self._compute_section_in_slot, model, sections, X
        )

        # Add constraints
        _timed("assignment_constraints", self._add_assignment_constraints, model, sections, X, Y)
        _timed("room_constraints", self._add_room_constraints, model, sections, X)
        _timed(
            "faculty_constraints",
            self._add_faculty_constraints, model, sections, X, Y, section_in_slot,
        )
        _timed(
            "linked_course_constraints",
            self._add_linked_course_constraints, model, sections, X, section_in_slot,
        )
        _timed("capacity_constraints", self._add_capacity_constraints, model, sections, X, Z)
        _timed(
            "graduation_critical_constraints",
            self._add_graduation_critical_constraints, model, sections, Z,
        )
        _timed(
            "single_enrollment_constraints",
            self._add_single_enrollment_constraints, model, sections, Z,
        )
        _timed(
            "crosslist_constraints", self._add_crosslist_link_constraints, model, sections, X, Y, Z
        )
        _timed(
            "intra_section_constraints",
            self._add_intra_section_constraints, model, sections, X, section_in_slot,
        )
        _timed(
            "faculty_travel_time_constraints",
            self._add_faculty_travel_time_constraints, model, sections, X, Y,
        )
        _timed(
            "symmetry_breaking",
            self._add_sibling_symmetry_breaking, model, sections, section_in_slot,
        )

        # Warm start. Big/tight instances first solve a placement-only model
        # (phase 1) and feed its solution back as a near-complete hint —
        # CP-SAT then optimizes instead of hunting for first feasibility.
        # Smaller instances use the cheap greedy hint directly.
        two_phase_cfg = self.config.solver_settings.get("two_phase", "auto")
        threshold = int(self.config.solver_settings.get("two_phase_threshold", 120))
        engage_two_phase = two_phase_cfg is True or (
            two_phase_cfg == "auto" and len(sections) >= threshold
        )
        used_two_phase = False
        phase1_elapsed = 0.0
        if self._repair_anchor:
            # Repair mode: the published schedule IS the warm start — hint it
            # verbatim and skip both the placement phase and the greedy hint.
            _timed("repair_anchor_hint", self._hint_from_anchor,
                   model, sections, X, Y, Z)
        else:
            if engage_two_phase:
                phase_budget = min(
                    90.0, float(self.config.solver_settings["max_time_seconds"]) * 0.25
                )
                _p0 = datetime.now()
                phase1 = _timed(
                    "placement_phase", self._solve_placement_phase, sections, phase_budget
                )
                phase1_elapsed = (datetime.now() - _p0).total_seconds()
                if phase1 is not None:
                    x_keys, y_keys = phase1
                    _timed("warm_start_from_placement", self._hint_from_placement,
                           model, sections, X, Y, Z, x_keys, y_keys)
                    used_two_phase = True
            if not used_two_phase:
                _timed("placement_hint", self._add_placement_hint, model, sections, X, Y)

        # Soft conflict penalties: penalize sections of high-conflict course pairs being in
        # overlapping slots. Hard conflict constraints cause infeasibility because the
        # near-complete conflict graph (30 courses, 411 edges) needs ~30 colors but only ~15
        # independent time groups exist. The joint model therefore keeps course-pair soft
        # penalties for tractability (per-student overlap clauses would number 68M+);
        # per-student overlap is enforced exactly by the phase-B sectioning pass in
        # _extract_solution.
        conflict_penalty_terms = _timed(
            "conflict_soft_penalties",
            self._build_conflict_soft_penalties,
            model, sections, section_in_slot,
        )
        # Faculty prep/teaching-day terms share the (weight, var) format —
        # ride the same objective plumbing.
        conflict_penalty_terms += _timed(
            "faculty_quality_penalties",
            self._add_faculty_quality_penalties,
            model, sections, Y, section_in_slot,
        )
        if self._repair_anchor:
            conflict_penalty_terms += _timed(
                "repair_anchor_terms",
                self._add_repair_anchor_terms,
                model, sections, X, Y, Z, section_in_slot,
            )

        # Set objective (includes conflict_penalty_terms)
        def _set_obj():
            return self._set_objective(model, sections, X, Y, Z, section_in_slot,
                                       conflict_penalty_terms=conflict_penalty_terms)
        _timed("set_objective", _set_obj)

        build_elapsed = (datetime.now() - build_start).total_seconds()
        model_stats = {
            "model_vars": len(model.proto.variables),
            "model_constraints": len(model.proto.constraints),
            "build_seconds": round(build_elapsed, 2),
        }
        build_msg = (
            f"Model built in {build_elapsed:.1f}s: "
            f"{model_stats['model_vars']} vars, "
            f"{model_stats['model_constraints']} constraints"
        )
        logger.info(build_msg)
        if log_callback:
            log_callback(build_msg)

        # Configure solver
        solver = cp_model.CpSolver()
        time_limit = float(self.config.solver_settings["max_time_seconds"])
        if phase1_elapsed:
            # Phase 1 spent part of the budget; the full solve gets the rest
            # (with a floor so a slow phase 1 can't starve it entirely).
            time_limit = max(30.0, time_limit - phase1_elapsed)
        solver.parameters.max_time_in_seconds = time_limit
        num_workers = resolve_worker_count(self.config.solver_settings["num_workers"])
        solver.parameters.num_workers = num_workers
        # Optional CP-SAT tuning knobs (unset = library default).
        lin_level = self.config.solver_settings.get("linearization_level")
        if lin_level is not None:
            solver.parameters.linearization_level = int(lin_level)

        if log_callback:
            # TUI mode: disable verbose solver output (it garbles the display).
            # Our own _timed() progress messages are sufficient.
            solver.parameters.log_search_progress = False
        else:
            # CLI/headless mode: verbose solver output goes to stdout.
            solver.parameters.log_search_progress = self.config.solver_settings.get(
                "log_search_progress", True
            )

        start_msg = f"Starting solver with {num_workers} workers, time limit {time_limit:.0f}s..."
        logger.info(start_msg)
        if log_callback:
            log_callback(start_msg)

        # Live visualization. attach() is a no-op (returns None, None) when
        # viz is disabled or no emitter was supplied, and in that case the
        # solve below runs exactly as it did before this existed. It must be
        # attached AFTER the log_search_progress settings above, since it
        # redirects the solver log into its own parser rather than stdout.
        from src.core.solver_viz import attach as _attach_viz

        viz_state, viz_callback = _attach_viz(
            solver, viz_emit, sections, section_in_slot, self.time_slots,
            time_limit, enabled=viz,
        )
        if log_callback and viz_state is None:
            log_callback("  [dim]Solver running... this may take 1-2 minutes.[/dim]")

        # Solve
        if viz_callback is not None:
            status = solver.Solve(model, viz_callback)
        else:
            status = solver.Solve(model)

        result.solve_time_seconds = (datetime.now() - start_time).total_seconds()
        result.stats = dict(model_stats)

        # Process results
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            result.status = "optimal" if status == cp_model.OPTIMAL else "feasible"
            result.objective_value = solver.ObjectiveValue()

            # Create schedule version
            schedule = ScheduleVersion(
                name=schedule_name,
                created_at=datetime.now(),
                semester=semester,
                status="draft",
                solver_stats={
                    "status": result.status,
                    "objective_value": result.objective_value,
                    "solve_time_seconds": result.solve_time_seconds,
                },
            )
            self.session.add(schedule)
            self.session.commit()

            # Extract solution
            result.sections, result.enrollments, result.meetings = self._extract_solution(
                solver, sections, X, Y, Z, schedule.id
            )

            # Optimality gap: how far the found solution could be from the
            # best possible (0% == proven optimal).
            bound = solver.BestObjectiveBound()
            gap_pct = (
                abs(bound - result.objective_value)
                / max(1.0, abs(result.objective_value)) * 100.0
            )
            schedule.solver_stats = {
                **schedule.solver_stats,
                "optimality_gap_pct": round(gap_pct, 2),
                "best_objective_bound": bound,
            }
            self.session.add(schedule)
            self.session.commit()

            result.stats = {
                **model_stats,
                "best_bound": bound,
                "sectioning": self._last_sectioning,
                "sections_scheduled": len(result.sections),
                "enrollments_created": len(result.enrollments),
                "meetings_created": len(result.meetings),
                "optimality_gap_pct": round(gap_pct, 2),
                "two_phase": used_two_phase,
                "sections_closed": len(sections) - len(result.sections),
                "repair_mode": bool(self._repair_anchor),
            }
            if result.stats["sections_closed"]:
                closed_msg = (
                    f"Solver closed {result.stats['sections_closed']} "
                    f"under-enrolled section(s) and consolidated their students."
                )
                logger.info(closed_msg)
                if log_callback:
                    log_callback(closed_msg)

            logger.info(
                f"Solver finished: {result.status}, "
                f"objective={result.objective_value:.2f}, "
                f"time={result.solve_time_seconds:.2f}s"
            )

        elif status == cp_model.INFEASIBLE:
            result.status = "infeasible"
            logger.error("Problem is infeasible - no valid schedule exists")
            msg = "Diagnosing which constraint families conflict…"
            if log_callback:
                log_callback(msg)
            result.infeasible_core = self._diagnose_infeasibility(sections)
            if result.infeasible_core:
                summary = "Conflicting constraint families: " + ", ".join(
                    result.infeasible_core
                )
                logger.error(summary)
                if log_callback:
                    log_callback(summary)
                    for fam in result.infeasible_core:
                        hint = INFEASIBILITY_HINTS.get(fam)
                        if hint:
                            log_callback(f"  {fam}: {hint}")

        elif status == cp_model.MODEL_INVALID:
            result.status = "invalid"
            logger.error("Model is invalid - check constraints")

        else:
            result.status = "unknown"
            logger.warning(f"Solver returned unknown status: {status}")
            advisory = (
                "No schedule found within the time budget — the model is "
                "likely over-tight rather than broken. Try fewer sections "
                "(raise section_settings.students_per_section or lower "
                "max_sections_per_course), a longer time limit, or approve "
                "more instructors for high-demand courses."
            )
            result.staffing_warnings = list(result.staffing_warnings) + [advisory]
            logger.warning(advisory)
            if log_callback:
                log_callback(f"[yellow]{advisory}[/yellow]")

        if viz_state is not None:
            viz_state.finish(
                result.status,
                objective=result.objective_value,
                gap_pct=result.stats.get("optimality_gap_pct"),
            )

        return result

    def re_solve(
        self,
        schedule_id: int,
        schedule_name: str | None = None,
        log_callback: "Callable[[str], None] | None" = None,
        viz_emit: "Callable[..., None] | None" = None,
        viz: "bool | None" = None,
    ) -> SolverResult:
        """Re-solve a schedule, preserving pinned section assignments.

        Loads sections from an existing schedule, keeps pinned assignments fixed,
        clears unpinned assignments, and runs the solver again. Creates a new
        ScheduleVersion rather than mutating the original.

        Args:
            schedule_id: ID of the existing schedule to re-solve.
            schedule_name: Name for the new schedule version.
            log_callback: Optional callback receiving solver log lines.

        Returns:
            SolverResult with the re-optimized sections.
        """
        # Load original schedule
        original = self.session.get(ScheduleVersion, schedule_id)
        if not original:
            result = SolverResult()
            result.status = "invalid"
            return result

        # Load all sections from the original schedule
        sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == schedule_id)
            ).all()
        )

        if not sections:
            result = SolverResult()
            result.status = "invalid"
            return result

        # Load SectionMeeting records for pinned sections
        self._pinned_meetings = {}
        for s in sections:
            if s.is_pinned and s.id is not None:
                meetings = list(
                    self.session.exec(
                        select(SectionMeeting)
                        .where(SectionMeeting.section_id == s.id)
                        .order_by(SectionMeeting.meeting_sequence)  # type: ignore
                    ).all()
                )
                if meetings:
                    self._pinned_meetings[s.crn] = [
                        (m.time_slot_id, m.room_id) for m in meetings
                    ]

        # Build new sections list - pinned keep assignments, unpinned are cleared
        new_sections = []
        for s in sections:
            new_section = Section(
                crn=s.crn,
                course_code=s.course_code,
                section_number=s.section_number,
                capacity=s.capacity,
                is_honors=s.is_honors,
                is_pinned=s.is_pinned,
                pin_reason=s.pin_reason,
                schedule_id=0,  # Will be set by solve()
            )
            if s.is_pinned:
                new_section.faculty_id = s.faculty_id
            new_sections.append(new_section)

        # Solve with the mixed pinned/unpinned section list
        name = schedule_name or f"Re-optimized: {original.name}"
        result = self.solve(
            new_sections, name, original.semester, log_callback=log_callback,
            viz_emit=viz_emit, viz=viz,
        )

        # Clear pinned meetings after solve
        self._pinned_meetings = {}
        return result

    def repair_solve(
        self,
        schedule_id: int,
        schedule_name: str | None = None,
        log_callback: "Callable[[str], None] | None" = None,
        viz_emit: "Callable[..., None] | None" = None,
        viz: "bool | None" = None,
    ) -> SolverResult:
        """Minimal-change re-solve ("repair") anchored to a published schedule.

        For post-publication breakage — an instructor resigns, a room goes
        offline, an approval changes — re-solve against CURRENT data while
        treating every assignment in the base schedule as strongly sticky:
        heavy stability rewards (soft_constraints["repair_anchor"]) mean the
        solver only moves what the disruption forces, instead of producing an
        unrecognizable fresh schedule. Pinned sections stay hard-pinned
        exactly as in re_solve. Writes a NEW ScheduleVersion; the base is
        untouched. Compare the two with the schedule diff to see what moved.

        Args:
            schedule_id: The published schedule to anchor to.
            schedule_name: Name for the new version (default "Repaired: …").
            log_callback: Optional callback receiving solver log lines.

        Returns:
            SolverResult; result.stats["repair_mode"] is True on success.
        """
        original = self.session.get(ScheduleVersion, schedule_id)
        if not original:
            result = SolverResult()
            result.status = "invalid"
            return result

        sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == schedule_id)
            ).all()
        )
        if not sections:
            result = SolverResult()
            result.status = "invalid"
            return result

        section_ids = [s.id for s in sections if s.id is not None]
        meetings_by_sid: dict[int, list[SectionMeeting]] = defaultdict(list)
        for m in self.session.exec(
            select(SectionMeeting)
            .where(SectionMeeting.section_id.in_(section_ids))  # type: ignore[union-attr]
            .order_by(SectionMeeting.meeting_sequence)  # type: ignore[arg-type]
        ).all():
            meetings_by_sid[m.section_id].append(m)
        students_by_sid: dict[int, list[str]] = defaultdict(list)
        for e in self.session.exec(
            select(SectionEnrollment).where(
                SectionEnrollment.section_id.in_(section_ids)  # type: ignore[union-attr]
            )
        ).all():
            students_by_sid[e.section_id].append(e.student_id)

        # Hard pins behave exactly as in re_solve; everything else is
        # anchored softly via _repair_anchor.
        self._pinned_meetings = {}
        self._repair_anchor = {}
        for s in sections:
            ms = meetings_by_sid.get(s.id, []) if s.id is not None else []
            if s.is_pinned and ms:
                self._pinned_meetings[s.crn] = [
                    (m.time_slot_id, m.room_id) for m in ms
                ]
            self._repair_anchor[s.crn] = {
                "meetings": [(m.time_slot_id, m.room_id) for m in ms],
                "faculty_id": s.faculty_id,
                "students": students_by_sid.get(s.id, []) if s.id is not None else [],
            }

        new_sections = []
        for s in sections:
            new_section = Section(
                crn=s.crn,
                course_code=s.course_code,
                section_number=s.section_number,
                capacity=s.capacity,
                is_honors=s.is_honors,
                is_pinned=s.is_pinned,
                pin_reason=s.pin_reason,
                schedule_id=0,  # Will be set by solve()
            )
            if s.is_pinned:
                new_section.faculty_id = s.faculty_id
            new_sections.append(new_section)

        name = schedule_name or f"Repaired: {original.name}"
        try:
            result = self.solve(
                new_sections, name, original.semester, log_callback=log_callback,
                viz_emit=viz_emit, viz=viz,
            )
        finally:
            self._repair_anchor = None
            self._pinned_meetings = {}
        return result

    def _diagnose_infeasibility(
        self, sections: list[Section], time_limit: float = 30.0
    ) -> list[str] | None:
        """Identify which hard-constraint families jointly cause infeasibility.

        Rebuilds the model with each family gated behind an assumption
        literal, then asks CP-SAT for a sufficient set of assumptions for
        infeasibility (a near-minimal unsat core). Returns family keys from
        INFEASIBILITY_HINTS, or None if diagnosis was inconclusive.
        """
        try:
            # Diagnosis builds a brand-new model, so the constraint builders
            # must not see _open_vars from the (now dead) main model — those
            # BoolVars belong to a different CpModel and would poison the fresh
            # one. Same save/None/restore guard as _solve_placement_phase;
            # closure is a relaxation, not a hard-constraint family, so it has
            # no place in the diagnosis.
            saved_open_vars = self._open_vars
            self._open_vars = None
            try:
                proxy = _AssumptionProxy(cp_model.CpModel())
                X, Y, Z = self._create_variables(proxy, sections)
                section_in_slot = self._compute_section_in_slot(proxy, sections, X)

                proxy.family("assignment")
                self._add_assignment_constraints(proxy, sections, X, Y)
                proxy.family("room-no-overlap")
                self._add_room_constraints(proxy, sections, X)
                proxy.family("faculty-no-overlap")
                self._add_faculty_constraints(proxy, sections, X, Y, section_in_slot)
                proxy.family("linked-courses")
                self._add_linked_course_constraints(proxy, sections, X, section_in_slot)
                proxy.family("capacity")
                self._add_capacity_constraints(proxy, sections, X, Z)
                proxy.family("graduation-critical")
                self._add_graduation_critical_constraints(proxy, sections, Z)
                proxy.family("multi-meeting")
                self._add_intra_section_constraints(proxy, sections, X, section_in_slot)
                proxy.family("travel-time")
                self._add_faculty_travel_time_constraints(proxy, sections, X, Y)
            finally:
                self._open_vars = saved_open_vars

            model = proxy.model
            model.AddAssumptions(list(proxy.families.values()))

            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = time_limit
            solver.parameters.num_workers = 1  # assumption cores need 1 worker
            status = solver.Solve(model)
            if status != cp_model.INFEASIBLE:
                logger.warning(
                    "Infeasibility diagnosis inconclusive (status %s)", status
                )
                return None

            core_indices = set(solver.SufficientAssumptionsForInfeasibility())
            core = [
                name for name, lit in proxy.families.items()
                if lit.Index() in core_indices
            ]
            return core or None
        except Exception:
            logger.exception("Infeasibility diagnosis failed")
            return None

    def _extract_solution(
        self,
        solver: cp_model.CpSolver,
        sections: list[Section],
        X: dict,
        Y: dict,
        Z: dict,
        schedule_id: int,
    ) -> tuple[list[Section], list[SectionEnrollment], list[SectionMeeting]]:
        """Extract solution from solved model, creating SectionMeeting records."""
        scheduled_sections = []
        enrollments = []
        all_meetings = []

        # One pass over the X dict finds every assigned (slot, room) — the
        # old version re-scanned compatible_slots × compatible_rooms per
        # section twice (once discarding the result).
        assigned: dict[tuple[str, int], tuple[int, str]] = {}
        for (crn, k, slot_id, room_id), var in X.items():
            if solver.Value(var) == 1:
                assigned[crn, k] = (slot_id, room_id)

        sec_cfg = self.config.section_settings
        float_capacity = sec_cfg.get("capacity_mode", "fixed") == "room"
        global_cap = sec_cfg.get("max_class_size")
        open_vars = self._open_vars

        for section in sections:
            if open_vars is not None and solver.Value(open_vars[section.crn]) == 0:
                continue  # closed by the solver — not part of the schedule
            section.schedule_id = schedule_id
            course = self.course_by_code[section.course_code]

            # Find assigned faculty
            for faculty in self.approved_faculty.get(course.code, []):
                if (section.crn, faculty.id) in Y:
                    if solver.Value(Y[section.crn, faculty.id]) == 1:
                        section.faculty_id = faculty.id
                        break

            # In "room" capacity mode the class size floats to the room the
            # solver chose: store the tightest assigned room's capacity,
            # clamped by the course/global class-size cap.
            if float_capacity:
                room_caps = []
                for k in range(len(self.course_meetings.get(course.code, []))):
                    placement = assigned.get((section.crn, k))
                    if placement is not None:
                        room = self.room_by_id.get(placement[1])
                        if room is not None:
                            room_caps.append(room.capacity)
                if room_caps:
                    ceiling = course.max_class_size or global_cap
                    size = min(room_caps)
                    section.capacity = min(size, ceiling) if ceiling else size

            self.session.add(section)
            scheduled_sections.append(section)

        # Commit sections first to get IDs
        self.session.commit()

        # Create SectionMeeting records
        for section in scheduled_sections:
            course = self.course_by_code[section.course_code]
            meetings = self.course_meetings.get(course.code, [])
            for k in range(len(meetings)):
                placement = assigned.get((section.crn, k))
                if placement is None:
                    continue
                slot_id, room_id = placement
                sm = SectionMeeting(
                    section_id=section.id,
                    meeting_sequence=k,
                    time_slot_id=slot_id,
                    room_id=room_id,
                )
                self.session.add(sm)
                all_meetings.append(sm)

        self.session.commit()

        # Extract enrollments. The joint model's Z is a first cut that may
        # seat a student in two sections meeting at once (it carries no
        # per-student overlap rule). The exact sectioning pass (phase B,
        # sectioning.py) re-seats on the now-fixed timetable with that rule
        # hard, using Z as its warm start.
        section_by_crn = {s.crn: s for s in scheduled_sections}
        joint_seats = {
            (student_id, crn)
            for (student_id, crn), var in Z.items()
            if crn in section_by_crn and solver.Value(var) == 1
        }
        seats = joint_seats
        self._last_sectioning = None
        if self.config.solver_settings.get("exact_sectioning", True):
            seats = self._exact_sectioning(scheduled_sections, assigned, joint_seats)
        for student_id, crn in sorted(seats):
            enrollment = SectionEnrollment(
                section_id=section_by_crn[crn].id,
                student_id=student_id,
                is_confirmed=False,
            )
            self.session.add(enrollment)
            enrollments.append(enrollment)

        self.session.commit()

        return scheduled_sections, enrollments, all_meetings

    def _exact_sectioning(
        self,
        scheduled_sections: list[Section],
        assigned: dict[tuple[str, int], tuple[int, str]],
        joint_seats: set[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        """Phase B: re-seat students on the solved timetable (sectioning.py).

        Keeps the joint model's seats if the pass finds nothing within
        solver.sectioning_time_seconds. Records what happened in
        self._last_sectioning (surfaced as result.stats["sectioning"]).
        """
        respect_rooms = self.config.hard_constraints.get("respect_room_capacity", True)
        pack_of = {
            s.crn: f"{gid}:{num}"
            for (gid, num), pack in self._crosslist_packs(scheduled_sections).items()
            for s in pack
        }
        specs = []
        for s in scheduled_sections:
            n_meetings = len(self.course_meetings.get(s.course_code, []))
            placements = [
                p for k in range(n_meetings) if (p := assigned.get((s.crn, k))) is not None
            ]
            cap = s.capacity
            if respect_rooms:
                room_caps = [
                    self.room_by_id[room_id].capacity
                    for _slot_id, room_id in placements
                    if room_id in self.room_by_id
                ]
                if room_caps:
                    cap = min(cap, min(room_caps))
            specs.append(SectionSpec(
                crn=s.crn,
                course_code=s.course_code,
                capacity=cap,
                slot_ids=tuple(slot_id for slot_id, _room_id in placements),
                pack=pack_of.get(s.crn),
            ))

        seat_anchor = None
        if self._repair_anchor:
            seat_w = int(
                self.config.soft_constraints.get("repair_anchor", {}).get("seat", 100)
            )
            seat_anchor = {
                (sid, crn): seat_w
                for crn, anchor in self._repair_anchor.items()
                for sid in anchor.get("students", [])
            }
        grad_mode = (
            "hard"
            if self.config.hard_constraints.get("graduation_critical_required", True)
            else "off"
        )
        settings = self.config.solver_settings
        outcome = section_students(
            specs, self.demands, self.slot_cliques,
            hint=joint_seats,
            seat_anchor=seat_anchor,
            time_limit_seconds=float(settings.get("sectioning_time_seconds", 120)),
            num_workers=resolve_worker_count(settings.get("num_workers", 0)),
            graduation_critical=grad_mode,
            relative_gap_limit=settings.get("sectioning_gap_limit", 0.005),
            linearization_level=settings.get("sectioning_linearization_level"),
            use_lp=bool(settings.get("sectioning_lp", True)),
        )
        joint_overlaps = count_student_overlaps(joint_seats, specs, self.slot_cliques)
        stats = {
            "mode": "exact" if outcome.ok else "joint-fallback",
            "status": outcome.status,
            "seats_joint": len(joint_seats),
            "seats": len(outcome.seats) if outcome.ok else len(joint_seats),
            "joint_overlaps_removed": joint_overlaps if outcome.ok else 0,
            "joint_overlaps": joint_overlaps,
            "unmet_graduation_critical": len(outcome.unmet_graduation_critical),
            "graduation_critical_relaxed": outcome.graduation_critical_relaxed,
            "solve_time_seconds": round(outcome.solve_time_seconds, 2),
            "gap_pct": outcome.gap_pct,
            "lp_bound": outcome.lp_bound,
            "method": outcome.method,
        }
        self._last_sectioning = stats
        if not outcome.ok:
            # Shapes only in logs (FERPA): counts, never student ids.
            logger.warning(
                "Exact sectioning returned %s — keeping the joint model's seats "
                "(%d student time overlap(s) remain)", outcome.status, joint_overlaps,
            )
            return joint_seats
        if outcome.unmet_graduation_critical:
            logger.warning(
                "Exact sectioning could not seat %d graduation-critical demand(s) "
                "on this timetable", len(outcome.unmet_graduation_critical),
            )
        logger.info(
            "Exact sectioning: %d seats (joint model: %d seats, %d overlap(s) removed)",
            len(outcome.seats), len(joint_seats), joint_overlaps,
        )
        return outcome.seats

