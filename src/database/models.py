"""SQLModel definitions for Gradimize database schema."""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import JSON, Column, Field, SQLModel


class StructureType(str, Enum):
    """Course structure types determining scheduling constraints."""

    STANDALONE = "STANDALONE"  # Normal course, no linking requirements
    COMBINED = "COMBINED"  # Lecture + Lab in single CRN
    PARENT = "PARENT"  # Lecture component with linked lab sections
    CHILD = "CHILD"  # Lab component linked to a PARENT


class RoomType(str, Enum):
    """Room capability types for matching courses to rooms."""

    LECTURE = "LECTURE"
    LAB_WET = "LAB_WET"
    LAB_DRY = "LAB_DRY"
    LAB_BIO = "LAB_BIO"
    LAB_CHEM = "LAB_CHEM"
    SEMINAR = "SEMINAR"
    AUDITORIUM = "AUDITORIUM"


class ClassStanding(str, Enum):
    """Student class standing for urgency calculation."""

    FR = "FR"  # Freshman
    SO = "SO"  # Sophomore
    JR = "JR"  # Junior
    SR = "SR"  # Senior


class RequirementGroupType(str, Enum):
    """Types of requirement groups for complex degree requirements."""

    OR = "OR"  # Pick one from list (e.g., "EDUC 1110 OR PHIL 1110")
    AND = "AND"  # All courses required (simple group)
    PICK_N = "PICK_N"  # Pick N courses from list (e.g., "choose 2 from...")
    MIN_CREDITS = "MIN_CREDITS"  # Need minimum credits from list (e.g., "24-28 credits from...")


# ─────────────────────────────────────────────────────────────────
# Core Entities
# ─────────────────────────────────────────────────────────────────


class TimeSlot(SQLModel, table=True):
    """A discrete scheduling block (e.g., MWF 8:00-8:50)."""

    id: int = Field(primary_key=True)
    name: str  # "MWF_0800" for display
    pattern: str  # "MWF", "TR", "M", etc.
    days: list[str] = Field(sa_column=Column(JSON))  # ["M", "W", "F"]
    start_time: str  # "08:00" (ISO format)
    end_time: str  # "08:50"
    duration_minutes: int  # 50
    slot_type: str  # "MWF_50", "TR_75", "LAB_150"


class BlockedPeriod(SQLModel):
    """A blocked time period where no classes should be scheduled.

    Used for university-wide events like common hours, faculty meetings, etc.
    This is not a database table - it's loaded from configuration.
    """

    name: str  # "Faculty Common Hour"
    days: list[str]  # ["M", "W", "F"]
    start_time: str  # "12:30" (24h format)
    end_time: str  # "14:30"
    reason: Optional[str] = None  # "Faculty meetings and office hours"


class Room(SQLModel, table=True):
    """Physical space where classes meet."""

    id: str = Field(primary_key=True)  # "SCI-201"
    building: str  # "Science Building"
    number: str  # "201"
    capacity: int
    room_type: RoomType
    equipment: list[str] = Field(default=[], sa_column=Column(JSON))
    is_active: bool = True


class Course(SQLModel, table=True):
    """Catalog entry (e.g., BIO 101)."""

    code: str = Field(primary_key=True)  # "BIO101"
    title: str  # "Introduction to Biology"
    credits: int
    structure_type: StructureType
    # If CHILD, points to PARENT. Declared FK only — PRAGMA foreign_keys stays
    # OFF (see connection.py), so this doesn't change enforcement.
    linked_course_code: Optional[str] = Field(default=None, foreign_key="course.code")
    required_room_types: list[str] = Field(
        default=["LECTURE"], sa_column=Column(JSON)
    )
    valid_slot_types: list[str] = Field(default=["MWF_50", "TR_75"], sa_column=Column(JSON))
    min_enrollment: int = 5  # Cancel if below this
    # Per-course class-size cap set by the department (None = use the global
    # section_settings default). In "room" capacity mode this bounds how far
    # a section's size may float up toward the assigned room's capacity.
    max_class_size: Optional[int] = None
    # Cross-listing: courses sharing a non-empty group label are ONE class
    # meeting under multiple codes (Banner XLST_GROUP) — the solver gives
    # paired sections the same slot, room, and instructor, and enforces the
    # room's capacity across their combined enrollment. Members should share
    # valid_slot_types / required_room_types / cadence (the doctor checks).
    crosslist_group: Optional[str] = None
    # Intensive course attributes (W=Writing, A=AA Heritage, I=Info Literacy, G=Global)
    attributes: list[str] = Field(default=[], sa_column=Column(JSON))
    # Multi-meeting patterns: list of per-meeting slot/room type constraints
    # Empty = single-meeting course using valid_slot_types/required_room_types
    # Example: [{"slot_types": ["T_120"], "room_types": ["LECTURE"]},
    #           {"slot_types": ["R_360"], "room_types": ["LAB_BIO"]}]
    meeting_patterns: list[dict] = Field(default=[], sa_column=Column(JSON))
    # Offering pattern (used as a soft prior by the scheduler).
    # Empty list means "any term"; otherwise restrict to listed terms.
    # ["FALL", "SPRING", "SUMMER"]
    offer_terms: list[str] = Field(default=[], sa_column=Column(JSON))
    # Cadence: "ANNUAL" (default), "BIENNIAL_EVEN" (even years only),
    # "BIENNIAL_ODD" (odd years only).
    cadence: str = "ANNUAL"


class Faculty(SQLModel, table=True):
    """Instructor."""

    id: str = Field(primary_key=True)  # "FSMITH01"
    name: str
    email: str
    max_credits: int = 12  # Per semester
    # Approved overload beyond max_credits for this person (None = use the
    # blanket hard_constraints.default_overload_credits). Only matters when
    # faculty_load_limit is enforced.
    max_overload_credits: Optional[int] = None
    approved_courses: list[str] = Field(
        default=[], sa_column=Column(JSON)
    )  # Course codes they can teach (empty = any)
    unavailable_slots: list[int] = Field(
        default=[], sa_column=Column(JSON)
    )  # TimeSlot IDs they cannot teach
    preferences: dict = Field(default={}, sa_column=Column(JSON))  # Soft preferences


class Student(SQLModel, table=True):
    """Enrolled student."""

    id: str = Field(primary_key=True)  # Banner ID
    name: str
    email: str
    class_standing: ClassStanding
    credits_earned: int
    major: str
    concentration: Optional[str] = None  # Track/concentration within major
    minor: Optional[str] = None
    is_honors: bool = False
    is_athlete: bool = False
    catalog_year: int  # Determines which requirements apply
    is_projected: bool = False  # True for synthetic freshmen projections
    # Week-shape preference for the seat-assignment rebalancer:
    # "cluster" (few days, tight packing — commuters/workers), "spread"
    # (breathing room between classes), or None = balanced default.
    # Collected via advising/registration and delivered as a students.csv
    # column; carries no identifying information so it survives
    # de-identification untouched.
    pacing_preference: Optional[str] = None
    # Registrar admit term (e.g. "202610" or "2026FA"), used to split
    # already-imported real incoming students from FreshmenProjection
    # synthetics so demand isn't double-counted. Optional: absent for
    # continuing students and for extracts that don't carry it yet.
    admit_term: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# Relationships & Derived Data
# ─────────────────────────────────────────────────────────────────


class Prerequisite(SQLModel, table=True):
    """Course dependency edge."""

    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(index=True, foreign_key="course.code")
    prereq_code: str = Field(foreign_key="course.code")
    min_grade: str = "D"  # Minimum grade required
    is_corequisite: bool = False  # Can be taken concurrently


class StudentCourseHistory(SQLModel, table=True):
    """Courses a student has completed."""

    id: Optional[int] = Field(default=None, primary_key=True)
    student_id: str = Field(index=True, foreign_key="student.id")
    course_code: str = Field(foreign_key="course.code")
    grade: str
    semester: str  # "2024FA"
    credits_earned: int


class FreshmenProjection(SQLModel, table=True):
    """Estimated incoming freshmen counts per major for demand projection."""

    id: Optional[int] = Field(default=None, primary_key=True)
    major: str
    estimated_count: int
    catalog_year: int
    semester: Optional[str] = None  # "2026FA" — None means unspecified
    is_active: bool = True


class StudentDemand(SQLModel, table=True):
    """Derived: Which courses each student needs, with priority scores."""

    id: Optional[int] = Field(default=None, primary_key=True)
    student_id: str = Field(index=True, foreign_key="student.id")
    course_code: str = Field(index=True, foreign_key="course.code")
    urgency_score: float  # Higher = more urgent
    is_required: bool  # vs elective
    is_graduation_critical: bool = False  # Must be satisfied (hard constraint)
    is_projected: bool = False  # True for synthetic freshmen projection demand
    reason: str  # "Major requirement", "Prereq for X"


class DegreeRequirement(SQLModel, table=True):
    """Courses required for a major/minor/concentration.

    Use this for simple 'this course is required' cases.
    For complex requirements (OR, pick-N, min-credits), use RequirementGroup.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    major: str  # "CSCI", "BIOL", etc.
    concentration: Optional[str] = None  # None = applies to all concentrations
    course_code: str = Field(foreign_key="course.code")
    is_required: bool = True  # False = elective option
    category: str = "core"  # "core", "elective", "general_ed", "concentration"
    catalog_year: int  # Requirements may change by year


class RequirementGroup(SQLModel, table=True):
    """A group of courses for complex requirements (OR, pick-N, min-credits).

    Examples:
    - "EDUC 1110 OR PHIL 1110" -> group_type=OR, required_count=1
    - "Major Track Elective 1-7" -> 7 groups, each with group_type=OR
    - "24-28 credits from track electives" -> group_type=MIN_CREDITS, min_credits=24
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    major: str  # "BIOL"
    concentration: Optional[str] = None  # "Pre-Medicine", "General Biology", etc.
    catalog_year: int
    group_name: str  # "Critical Skills", "Major Track Elective 1", etc.
    group_type: RequirementGroupType  # OR, AND, PICK_N, MIN_CREDITS
    category: str  # "general_ed", "core", "track_elective", "distribution"
    required_count: int = 1  # For OR/PICK_N: how many courses to pick
    min_credits: Optional[int] = None  # For MIN_CREDITS: minimum credits needed
    max_credits: Optional[int] = None  # For MIN_CREDITS: maximum credits (optional cap)


class RequirementGroupCourse(SQLModel, table=True):
    """A course option within a RequirementGroup.

    For paired courses like "BIOL 3304 WITH BIOL 3305", use paired_with_code
    to indicate both must be taken together to satisfy the requirement.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="requirementgroup.id")
    course_code: str = Field(foreign_key="course.code")
    # For paired requirements: "BIOL 3304 with BIOL 3305"
    paired_with_code: Optional[str] = Field(default=None, foreign_key="course.code")


class SemesterGuide(SQLModel, table=True):
    """A semester-by-semester course plan for a major/concentration."""

    id: Optional[int] = Field(default=None, primary_key=True)
    major: str
    concentration: Optional[str] = None
    catalog_year: int
    name: str
    created_by: Optional[str] = None


class SemesterGuideEntry(SQLModel, table=True):
    """A single entry in a semester guide (specific course or generic slot)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    guide_id: int = Field(foreign_key="semesterguide.id")
    semester_number: int  # 1-8 (1=FR fall, 2=FR spring, ...)
    course_code: Optional[str] = Field(default=None, foreign_key="course.code")
    category: Optional[str] = None  # "general_ed", "core", "track_elective", etc.
    credits: int = 3
    label: str


class AttributeRequirement(SQLModel, table=True):
    """Intensive course requirements (W, A, I, G designations).

    These are overlay requirements - courses count toward both their
    regular category AND toward attribute requirements.

    Example: ENGL 1112 might be both an "Essential Skills" course
    AND a "Writing Intensive (W)" course.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    major: str  # "BIOL" or "*" for all majors
    concentration: Optional[str] = None  # None = applies to all concentrations
    catalog_year: int
    attribute: str  # "W", "A", "I", "G"
    attribute_name: str  # "Writing Intensive", "A-A Heritage", etc.
    required_count: int  # 2 for Writing, 1 for A/I/G typically


# ─────────────────────────────────────────────────────────────────
# Schedule Output
# ─────────────────────────────────────────────────────────────────


class ScheduleVersion(SQLModel, table=True):
    """A complete schedule (for versioning/comparison)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str  # "Draft A", "Final"
    created_at: datetime
    semester: str  # "2025SP"
    status: str  # "draft", "approved", "archived"
    solver_stats: dict = Field(default={}, sa_column=Column(JSON))


class Section(SQLModel, table=True):
    """A scheduled offering of a course (solver output)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(index=True, foreign_key="scheduleversion.id")
    crn: str  # Unique for Banner export
    course_code: str = Field(foreign_key="course.code")
    section_number: str  # "001", "002"
    capacity: int
    is_honors: bool = False
    # Assigned by solver:
    faculty_id: Optional[str] = Field(default=None, foreign_key="faculty.id")
    # Manual overrides:
    is_pinned: bool = False  # Don't let solver move this
    pin_reason: Optional[str] = None
    # Finals scheduling: True (default) means a final exam will be scheduled
    # for this section. Set False to opt out (e.g., labs that don't need a final).
    needs_final: bool = Field(default=True)


class SectionMeeting(SQLModel, table=True):
    """A single meeting of a section (supports multi-meeting sections).

    For standard courses, there is one meeting per section.
    For multi-meeting courses (e.g., lecture T + lab R), there are multiple.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    section_id: int = Field(index=True, foreign_key="section.id")
    meeting_sequence: int  # 0, 1, 2...
    time_slot_id: int = Field(foreign_key="timeslot.id")
    room_id: str = Field(foreign_key="room.id")


class SectionEnrollment(SQLModel, table=True):
    """Student assigned to section (solver output)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    section_id: int = Field(index=True, foreign_key="section.id")
    student_id: str = Field(index=True, foreign_key="student.id")
    is_confirmed: bool = False  # vs tentative assignment


# ─────────────────────────────────────────────────────────────────
# Finals exam scheduling
# ─────────────────────────────────────────────────────────────────


class FinalsSchedule(SQLModel, table=True):
    """A finals exam schedule generated for a ScheduleVersion."""

    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(index=True, foreign_key="scheduleversion.id")
    name: str  # "Finals - Fall 2025"
    created_at: datetime
    status: str = "draft"  # "draft", "approved", "archived"
    # FinalsConfig snapshot at solve time (start_time, block_minutes, weights, etc.)
    config: dict = Field(default={}, sa_column=Column(JSON))
    solver_stats: dict = Field(default={}, sa_column=Column(JSON))


class FinalExamBlock(SQLModel, table=True):
    """A candidate finals exam block (e.g., Tue 8:30-10:20)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    finals_schedule_id: int = Field(index=True, foreign_key="finalsschedule.id")
    day: str  # "T", "W", "R", "F"
    ordinal: int  # 0..N-1 within the day
    start_time: str  # "08:30"
    end_time: str  # "10:20"
    label: str  # "Tue 8:30-10:20"


class FinalExamAssignment(SQLModel, table=True):
    """A section's assigned finals block (room reused from regular meeting).

    Either block_id is set (regular finals block) OR is_in_place=True with
    in_place_time_slot_id pointing at the section's normal meeting (per
    institutional policy: evening classes — start at/after
    FinalsConfig.evening_in_place_threshold, default 18:00 — and Saturday
    classes take their final at their regular meeting time).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    finals_schedule_id: int = Field(index=True, foreign_key="finalsschedule.id")
    section_id: int = Field(foreign_key="section.id")
    block_id: Optional[int] = Field(default=None, foreign_key="finalexamblock.id")
    room_id: str = Field(foreign_key="room.id")
    is_in_place: bool = False
    in_place_time_slot_id: Optional[int] = Field(
        default=None, foreign_key="timeslot.id"
    )
