"""Solver configuration and result types — the data side of the engine.

Extracted from ``engine.py`` so the objects a reader reaches for first (what
knobs exist, what a solve returns, the human-readable infeasibility hints)
live apart from the ~2,700-line CP-SAT model builder. Pure data/config: no
CP-SAT, no database session, no ``SchedulingEngine`` dependency. ``engine.py``
re-exports every public name here, so ``from src.core.engine import
SchedulerConfig`` (and SolverResult / compute_blocked_slot_ids / …) keeps
working.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.timeutil import times_overlap
from src.database.models import (
    BlockedPeriod,
    Room,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

logger = logging.getLogger(__name__)


@dataclass
class MeetingInfo:
    """Per-meeting compatibility info for solver variable creation."""

    compatible_slots: list[TimeSlot] = field(default_factory=list)
    compatible_rooms: list[Room] = field(default_factory=list)


class SchedulerConfig:
    """Configuration for the scheduler.

    Every hard-constraint flag here is honored by the engine. Student
    schedule overlap is intentionally NOT a constraint of the joint model
    (per-student overlap clauses explode at scale); there it is discouraged
    via the soft conflict penalties gated by `conflict_urgency_threshold`.
    Phase B (`exact_sectioning`) then enforces it exactly once the timetable
    is fixed.
    """

    # Loaded by load_default(), used by all production entry points.
    DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "constraints.yaml"

    def __init__(self, config_path: Path | str | None = None):
        """Load configuration.

        Args:
            config_path: Path to constraints.yaml. None means pure code
                defaults (used by tests); production callers should use
                SchedulerConfig.load_default().
        """
        self.hard_constraints = {
            "room_no_overlap": True,
            "faculty_no_overlap": True,
            "respect_room_capacity": True,
            "match_room_type": True,
            "faculty_approval_required": True,
            "linked_no_overlap": True,
            "min_travel_time": 10,
            "graduation_critical_required": True,
            # Blanket approved overload beyond each instructor's max_credits
            # (a Faculty.max_overload_credits entry overrides it per person).
            # Only consulted when faculty_load_limit is enforced.
            "default_overload_credits": 0,
        }

        self.soft_constraints = {
            "early_morning_penalty": {"hour": 8, "weight": -10},
            "late_afternoon_penalty": {"hour": 17, "weight": -10},
            # Applies per adjacent faculty slot pair (no max_consecutive knob).
            "back_to_back_penalty": {"faculty": {"weight": -20}},
            "faculty_preference_bonus": {"weight": 5},
            "day_balance_penalty": {"weight": -15},
            "room_balance_penalty": {"weight": -5},
            "faculty_load_balance_penalty": {"weight": -8},
            # Cost per OPEN section when allow_section_closure is on — a
            # gentle push to consolidate half-empty siblings. Keep the
            # magnitude below a typical urgency score so a student is never
            # sacrificed just to close a room.
            "open_section_penalty": {"weight": -5},
            # Per DISTINCT COURSE an instructor teaches beyond their first:
            # three sections of MATH101 is one prep; MATH101+MATH301+a lab
            # is three. 0 disables.
            "faculty_prep_penalty": {"weight": -15},
            # Per DISTINCT DAY an instructor teaches beyond their first:
            # rewards compact teaching schedules (TR teaching, MWF research).
            # 0 disables.
            "faculty_teaching_day_penalty": {"weight": -10},
            # Repair mode (repair_solve): reward for KEEPING each published
            # assignment, so only changes forced by the disruption happen.
            # Implemented as stability rewards on existing variables (a
            # reward for staying == a penalty for moving, without extra
            # model variables). Weights must dwarf ordinary objective
            # deltas: time moves disrupt every enrolled student, instructor
            # changes disrupt people, room moves are cheap. "seat" is small
            # on purpose — it breaks ties so students keep their planned
            # section, but a real conflict still reseats them.
            "repair_anchor": {
                "time": 50000,
                "instructor": 30000,
                "room": 10000,
                "seat": 100,
            },
        }

        self.conflict_urgency_threshold = 50.0

        # Demand source: "degreeworks" uses DegreeWorks audit CSV, "internal" uses
        # built-in DemandCalculator with DegreeRequirement/RequirementGroup tables.
        self.demand_source = "degreeworks"
        self.degreeworks_csv: str | None = None  # Path to DW courses-needed CSV

        self.solver_settings = {
            "max_time_seconds": 300,
            "num_workers": 0,
            "log_search_progress": False,
            "exact_sectioning": True,
            "sectioning_time_seconds": 120,
            "sectioning_lp": True,
            "symmetry_breaking": True,
            "no_overlap_formulation": "cliques",
        }

        # Off-cadence courses (offered in a different term or off-year per
        # Course.offer_terms / Course.cadence) need at least this many demand
        # rows to spawn a section. Default 10: students still get the course
        # if there's real pressure, but light demand won't force an off-cycle
        # offering.
        self.off_cadence_demand_threshold: int = 10

        # Section sizing knobs used by create_sections()
        self.section_settings = {
            "default_capacity": 30,     # seats per generated section
            "students_per_section": 25, # demand per additional section
            "max_sections_per_course": 5,
            "crn_start": 10001,         # first generated CRN
            # "fixed": sections seat default_capacity (or the course's
            # max_class_size). "room": class size floats — enrollment is
            # bounded by the assigned room (and any class-size cap), and the
            # stored section capacity reflects the room actually chosen.
            "capacity_mode": "fixed",
            # Global ceiling on floating class size in "room" mode
            # (None = the room is the only limit). A course's own
            # max_class_size always takes precedence over this.
            "max_class_size": None,
            # Let the solver CLOSE sections instead of running them
            # under-enrolled: an open section must seat at least the
            # course's min_enrollment (or all of its demand, whichever is
            # smaller — nobody is stranded to satisfy a minimum), and a
            # closed section frees its room and instructor entirely.
            # Cross-listed packs open and close together.
            "allow_section_closure": False,
        }

        self.blocked_periods: list[BlockedPeriod] = []

        if config_path:
            self._load_from_file(config_path)

    @classmethod
    def load_default(cls) -> "SchedulerConfig":
        """Config with config/constraints.yaml applied when it exists.

        This is what production entry points (web API, TUI, CLI) should use,
        so YAML-configured blocked periods, weights, and thresholds actually
        take effect. Falls back to code defaults if the file is absent.
        """
        if cls.DEFAULT_CONFIG_PATH.exists():
            return cls(cls.DEFAULT_CONFIG_PATH)
        return cls()

    def _load_from_file(self, config_path: Path | str):
        """Load config from YAML file."""
        config_path = Path(config_path)
        if not config_path.exists():
            logger.warning(f"Config file not found: {config_path}")
            return

        with open(config_path) as f:
            config = yaml.safe_load(f)

        if "hard_constraints" in config:
            self.hard_constraints.update(config["hard_constraints"])
        if "soft_constraints" in config:
            self.soft_constraints.update(config["soft_constraints"])
        if "conflict_urgency_threshold" in config:
            self.conflict_urgency_threshold = float(config["conflict_urgency_threshold"])
        if "solver" in config:
            self.solver_settings.update(config["solver"])
        if "blocked_periods" in config:
            self.blocked_periods = [
                BlockedPeriod(
                    name=bp["name"],
                    days=bp["days"],
                    start_time=bp["start_time"],
                    end_time=bp["end_time"],
                    reason=bp.get("reason"),
                )
                for bp in config["blocked_periods"]
            ]
            logger.info(f"Loaded {len(self.blocked_periods)} blocked periods")
        if "demand_source" in config:
            self.demand_source = config["demand_source"]
        if "degreeworks_csv" in config:
            self.degreeworks_csv = config["degreeworks_csv"]
        if "off_cadence_demand_threshold" in config:
            self.off_cadence_demand_threshold = int(
                config["off_cadence_demand_threshold"]
            )
        if "section_settings" in config:
            self.section_settings.update(config["section_settings"])
            mode = self.section_settings.get("capacity_mode", "fixed")
            if mode not in ("fixed", "room"):
                logger.warning(
                    f"Unknown capacity_mode '{mode}' — using 'fixed'"
                )
                self.section_settings["capacity_mode"] = "fixed"


class SolverResult:
    """Results from a solver run."""

    def __init__(self):
        self.status: str = "unknown"
        self.sections: list[Section] = []
        self.enrollments: list[SectionEnrollment] = []
        self.meetings: list[SectionMeeting] = []
        self.objective_value: float = 0.0
        self.solve_time_seconds: float = 0.0
        self.stats: dict[str, Any] = {}
        # When status == "infeasible": the (near-minimal) set of hard-constraint
        # families that jointly conflict, from a CP-SAT assumption core.
        self.infeasible_core: list[str] | None = None
        # Structural staffing bottlenecks detected before the solve (see
        # _staffing_preflight). Non-empty usually explains an "unknown" or
        # "infeasible" status without waiting for the solver to prove it.
        self.staffing_warnings: list[str] = []


def compute_blocked_slot_ids(
    time_slots: list[TimeSlot], blocked_periods: list[BlockedPeriod]
) -> set[int]:
    """Compute which time slots overlap with any blocked period.

    A slot is blocked if it shares at least one day with a blocked period
    AND the time ranges overlap.

    Args:
        time_slots: All available time slots.
        blocked_periods: List of blocked periods from config.

    Returns:
        Set of TimeSlot IDs that are blocked.
    """
    blocked_ids: set[int] = set()

    for slot in time_slots:
        slot_days = set(slot.days)
        for period in blocked_periods:
            period_days = set(period.days)
            # Check if they share any days
            if not slot_days & period_days:
                continue
            # Check if times overlap
            if times_overlap(
                slot.start_time, slot.end_time,
                period.start_time, period.end_time
            ):
                blocked_ids.add(slot.id)
                break  # No need to check other periods for this slot

    return blocked_ids


# Human guidance shown when a family appears in an infeasibility core.
INFEASIBILITY_HINTS = {
    "assignment": (
        "Some section has no valid (slot, room) placement at all — check room types, "
        "blocked periods, and pinned placements that may no longer exist."
    ),
    "room-no-overlap": (
        "Not enough distinct room-slots: too many sections compete for the same rooms at "
        "the same times. Add rooms, allow more slot types, or reduce sections."
    ),
    "faculty-no-overlap": (
        "A faculty member is required in two places at once (or has too little "
        "availability). Check teaching-qualification lists (approved_courses) and "
        "unavailable slots."
    ),
    "linked-courses": (
        "Parent/child (lecture/lab) sections cannot avoid overlapping — usually too few "
        "compatible slots for one of the pair."
    ),
    "capacity": (
        "Enrollment cannot fit: sections exceed every compatible room's capacity. Raise "
        "room capacity, lower section capacity, or add sections."
    ),
    "graduation-critical": (
        "Graduation-critical students cannot all be seated — usually capacity or too few "
        "sections of a required course."
    ),
    "multi-meeting": (
        "A multi-meeting course cannot place its meetings without overlapping itself — "
        "check its meeting patterns' slot types."
    ),
    "travel-time": (
        "Back-to-back placements force impossible building changes — relax min_travel_time "
        "or room choices."
    ),
}
