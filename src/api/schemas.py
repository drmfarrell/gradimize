"""Pydantic request/response schemas for the API."""

from typing import Any, Optional

from pydantic import BaseModel

# ─────────────────────────────────────────────────────────────────
# Schedule Schemas
# ─────────────────────────────────────────────────────────────────


class ScheduleSummary(BaseModel):
    """Summary of a schedule."""

    id: int
    name: str
    semester: str
    status: str
    created_at: str
    sections_count: int


class ScheduleDetail(BaseModel):
    """Detailed schedule information."""

    id: int
    name: str
    semester: str
    status: str
    created_at: str
    solver_stats: dict[str, Any]
    sections: list["SectionDetail"]


class MeetingDetail(BaseModel):
    """Details of a single meeting within a section."""

    meeting_sequence: int
    time_slot_id: int
    time_slot_name: str
    room_id: str
    room_name: str


class SectionDetail(BaseModel):
    """Details of a scheduled section."""

    id: int
    crn: str
    course_code: str
    section_number: str
    capacity: int
    time_slot_name: Optional[str]
    time_slot_id: Optional[int] = None
    room_id: Optional[str]
    room_name: Optional[str]
    faculty_name: Optional[str]
    faculty_id: Optional[str] = None
    enrollment_count: int
    is_pinned: bool = False
    pin_reason: Optional[str] = None
    meetings: list[MeetingDetail] = []


class MeetingPinItem(BaseModel):
    """Pin info for a single meeting."""

    meeting_sequence: int
    time_slot_id: Optional[int] = None
    room_id: Optional[str] = None


class SectionPinRequest(BaseModel):
    """Request to pin a section to specific assignments."""

    time_slot_id: Optional[int] = None
    room_id: Optional[str] = None
    faculty_id: Optional[str] = None
    pin_reason: Optional[str] = None
    meetings: list[MeetingPinItem] = []


class SectionPinResponse(BaseModel):
    """Response after pinning/unpinning a section."""

    section_id: int
    crn: str
    is_pinned: bool
    time_slot_name: Optional[str] = None
    room_id: Optional[str] = None
    faculty_name: Optional[str] = None
    pin_reason: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# Data Schemas
# ─────────────────────────────────────────────────────────────────


class CourseInfo(BaseModel):
    """Course information."""

    code: str
    title: str
    credits: int
    structure_type: str
    required_room_types: list[str]
    valid_slot_types: list[str]
    offer_terms: list[str] = []
    cadence: str = "ANNUAL"


class StudentInfo(BaseModel):
    """Student information."""

    id: str
    name: str
    class_standing: str
    major: str
    credits_earned: int


class FacultyInfo(BaseModel):
    """Faculty information."""

    id: str
    name: str
    email: str
    max_credits: int
    approved_courses: list[str]
    unavailable_slots: list[int] = []
    preferences: dict = {}


class FacultyPreferencesUpdate(BaseModel):
    """Request to update faculty scheduling preferences."""

    unavailable_slots: list[int] | None = None
    preferred_slots: list[int] | None = None
    allow_back_to_back: bool | None = None


class RoomInfo(BaseModel):
    """Room information."""

    id: str
    building: str
    number: str
    capacity: int
    room_type: str


# ─────────────────────────────────────────────────────────────────
# Conflict Schemas
# ─────────────────────────────────────────────────────────────────


class ConflictPair(BaseModel):
    """A pair of courses with conflict score."""

    course1: str
    course2: str
    conflict_score: float


class ConflictMatrixResponse(BaseModel):
    """Response with conflict matrix data."""

    total_courses: int
    high_conflict_pairs: list[ConflictPair]
    threshold: float


# ─────────────────────────────────────────────────────────────────
# Demand Schemas
# ─────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────
# Export Schemas
# ─────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────
# Statistics Schemas
# ─────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────
# Projection Schemas
# ─────────────────────────────────────────────────────────────────


class ProjectionInfo(BaseModel):
    """Freshmen enrollment projection info."""

    id: int
    major: str
    estimated_count: int
    catalog_year: int
    semester: str
    is_active: bool


class ProjectionCreateRequest(BaseModel):
    """Request to create/update a freshmen projection."""

    major: str
    estimated_count: int
    catalog_year: int
    semester: str = ""


# ─────────────────────────────────────────────────────────────────
# Semester Guide Schemas
# ─────────────────────────────────────────────────────────────────


class SystemStats(BaseModel):
    """Overall system statistics."""

    total_students: int
    total_courses: int
    total_faculty: int
    total_rooms: int
    total_time_slots: int
    total_demands: int
    schedules_count: int


# ─────────────────────────────────────────────────────────────────
# Import Schemas
# ─────────────────────────────────────────────────────────────────


class ScheduleImportRequest(BaseModel):
    csv_content: str
    schedule_name: str = "Imported Schedule"
    semester: str = "2025SP"


class ScheduleImportResponse(BaseModel):
    schedule_id: int | None
    sections_imported: int
    meetings_imported: int
    warnings: list[str]
    errors: list[str]


# ─────────────────────────────────────────────────────────────────
# Recommendation Schemas
# ─────────────────────────────────────────────────────────────────


class RecommendationItem(BaseModel):
    type: str
    priority: float
    course_code: str | None
    section_crn: str | None
    description: str
    suggested_action: str


class RecommendationResponse(BaseModel):
    schedule_id: int
    total_recommendations: int
    recommendations: list[RecommendationItem]
