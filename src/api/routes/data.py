"""Data-related API routes (students, courses, faculty, etc.)."""

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, func, select

from src.api.schemas import (
    CourseInfo,
    FacultyInfo,
    FacultyPreferencesUpdate,
    RoomInfo,
    StudentInfo,
    SystemStats,
)
from src.core import job_lock
from src.core.offerings import infer_course_offerings
from src.core.uploads import IMPORT_KINDS, IMPORT_KINDS_BY_KEY, import_data_file
from src.database.connection import DEFAULT_DB_PATH, get_session
from src.database.models import (
    Course,
    Faculty,
    Room,
    ScheduleVersion,
    Student,
    StudentDemand,
    TimeSlot,
)

# The replanner is path-based (it scratch-copies the DB rather than using
# the request session); module-level so tests can monkeypatch it.
REPLAN_DB_PATH = DEFAULT_DB_PATH

router = APIRouter(prefix="/data", tags=["data"])


# ─────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────


def _count(session: Session, model) -> int:
    return session.exec(select(func.count()).select_from(model)).one()


@router.get("/stats", response_model=SystemStats)
def get_system_stats(session: Session = Depends(get_session)):
    """Get overall system statistics.

    COUNT(*) per table — this endpoint is polled by the dashboard and used
    to materialize entire tables (all students, all demands, …) just to
    len() them, blocking the event loop for every viewer.
    """
    return SystemStats(
        total_students=_count(session, Student),
        total_courses=_count(session, Course),
        total_faculty=_count(session, Faculty),
        total_rooms=_count(session, Room),
        total_time_slots=_count(session, TimeSlot),
        total_demands=_count(session, StudentDemand),
        schedules_count=_count(session, ScheduleVersion),
    )


class HealthCheckResult(BaseModel):
    name: str
    status: str  # "ok" | "warn" | "fail"
    summary: str
    count: int
    details: list[str]


class HealthReport(BaseModel):
    status: str  # worst status across results
    results: list[HealthCheckResult]


@router.get("/health", response_model=HealthReport)
def data_health(  # sync def → threadpool; the deep check can take seconds
    deep: bool = True,
    session: Session = Depends(get_session),
):
    """Run the data-health doctor (see src/core/doctor.py).

    `deep=false` skips the solver-backed staffing pre-flight for a fast
    integrity-only pass.
    """
    from src.core.doctor import overall_status, run_health_checks

    results = run_health_checks(session, deep=deep)
    return HealthReport(
        status=overall_status(results),
        results=[HealthCheckResult(**r.as_dict()) for r in results],
    )


# ─────────────────────────────────────────────────────────────────
# Data file import (uploads)
# ─────────────────────────────────────────────────────────────────


class ImportKindInfo(BaseModel):
    key: str
    label: str
    filename: str
    description: str
    is_yaml: bool


class ImportFileResponse(BaseModel):
    kind: str
    filename: str
    rows_loaded: int
    cleared: bool
    errors: list[str]


@router.get("/import/kinds", response_model=list[ImportKindInfo])
async def list_import_kinds():
    """List the data file kinds the solver can ingest, in recommended order."""
    return [
        ImportKindInfo(
            key=k.key,
            label=k.label,
            filename=k.filename,
            description=k.description,
            is_yaml=k.is_yaml,
        )
        for k in IMPORT_KINDS
    ]


# Generous for CSV/YAML extracts, but bounded: uvicorn has no default body
# limit, so an unchecked read() would buffer arbitrarily large uploads in RAM.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.post("/import/{kind}", response_model=ImportFileResponse)
def import_file(  # sync def → threadpool; heavy ingest must not block the loop
    kind: str,
    file: UploadFile = File(...),
    replace: bool = Query(True),
    session: Session = Depends(get_session),
):
    """Upload and import a single solver input file.

    Replace-by-default: re-uploading wipes existing rows of this kind and
    reloads from scratch. Validation problems come back in `errors` with a
    200 rather than failing the request, so the UI can show them per file.
    """
    if kind not in IMPORT_KINDS_BY_KEY:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown import kind '{kind}'. Valid: {list(IMPORT_KINDS_BY_KEY)}",
        )
    # Hold the optimization slot for the whole import, not just a pre-check: a
    # bare status() read leaves a window where a solve can start right after the
    # check and race the wipe+reload. Acquire, and release in finally.
    if not job_lock.try_acquire("import", f"import {kind}"):
        raise HTTPException(
            status_code=409,
            detail="An optimization is running; imports would race the solver. "
            "Try again when it finishes.",
        )
    try:
        content = file.file.read(MAX_UPLOAD_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
            )
        result = import_data_file(session, kind, content, replace=replace)
    finally:
        job_lock.release()
    return ImportFileResponse(
        kind=result.kind,
        filename=result.filename,
        rows_loaded=result.rows_loaded,
        cleared=result.cleared,
        errors=result.errors,
    )


# ─────────────────────────────────────────────────────────────────
# Courses
# ─────────────────────────────────────────────────────────────────


@router.get("/courses", response_model=list[CourseInfo])
async def list_courses(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """List all courses."""
    courses = session.exec(select(Course).offset(skip).limit(limit)).all()
    return [
        CourseInfo(
            code=c.code,
            title=c.title,
            credits=c.credits,
            structure_type=c.structure_type.value,
            required_room_types=c.required_room_types,
            valid_slot_types=c.valid_slot_types,
            offer_terms=c.offer_terms,
            cadence=c.cadence,
        )
        for c in courses
    ]


class OfferingsInferRequest(BaseModel):
    biennial_min_years: int = 2
    apply: bool = True


class OfferingsInferChange(BaseModel):
    course_code: str
    observed_terms: list[str]
    observed_years: list[int]
    new_offer_terms: list[str]
    new_cadence: str
    note: str | None = None


class OfferingsInferResponse(BaseModel):
    courses_examined: int
    courses_with_history: int
    courses_changed: int
    schedules_examined: int
    skipped_unparseable_semesters: list[str]
    changes: list[OfferingsInferChange]


@router.post("/courses/infer-offerings", response_model=OfferingsInferResponse)
def infer_offerings(
    request: OfferingsInferRequest = OfferingsInferRequest(),
    session: Session = Depends(get_session),
):
    """Infer Course.offer_terms and cadence from historical schedules.

    Walks every ScheduleVersion + Section row and rewrites each course's
    offer_terms (terms ever observed) and cadence (annual / biennial-even /
    biennial-odd, based on the parity of years observed). Set apply=False
    for a dry run.
    """
    report = infer_course_offerings(
        session,
        biennial_min_years=request.biennial_min_years,
        apply=request.apply,
    )
    changes = [
        OfferingsInferChange(
            course_code=r.course_code,
            observed_terms=sorted(r.observed_terms),
            observed_years=sorted(r.observed_years),
            new_offer_terms=r.new_offer_terms,
            new_cadence=r.new_cadence,
            note=r.note,
        )
        for r in report.results
        if r.changed
    ]
    return OfferingsInferResponse(
        courses_examined=report.courses_examined,
        courses_with_history=report.courses_with_history,
        courses_changed=report.courses_changed,
        schedules_examined=report.schedules_examined,
        skipped_unparseable_semesters=report.skipped_unparseable_semesters,
        changes=changes,
    )


# ─────────────────────────────────────────────────────────────────
# Students
# ─────────────────────────────────────────────────────────────────


@router.get("/students", response_model=list[StudentInfo])
async def list_students(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    major: str | None = None,
    session: Session = Depends(get_session),
):
    """List students with optional filtering."""
    stmt = select(Student)
    if major:
        stmt = stmt.where(Student.major == major)
    stmt = stmt.offset(skip).limit(limit)

    students = session.exec(stmt).all()
    return [
        StudentInfo(
            id=s.id,
            name=s.name,
            class_standing=s.class_standing.value,
            major=s.major,
            credits_earned=s.credits_earned,
        )
        for s in students
    ]


@router.get("/students/{student_id}/replan")
def student_replan(
    student_id: str,
    start: str = Query(..., description="First term to plan, e.g. 2026FA"),
    terms: int = Query(12, ge=1, le=24),
    credits: int = Query(15, ge=3, le=24, description="Credits per term"),
    as_markdown: bool = Query(True),
):
    """One-student replanner: fastest remaining path to graduation.

    "Failed MATH102 — what now?" Simulates the student term-by-term against
    the catalog's offering patterns (prereqs, offer_terms, biennial
    cadences): each term takes their most urgent available courses up to the
    credit load, marks them complete, and advances — until requirements run
    out (graduation term) or `terms` is exhausted. Runs on a throwaway copy
    of the database; live data is never touched. Seats are assumed available
    (catalog-level planning) — cross-check against the term's real schedule.
    """
    from src.core.replan import render_replan_markdown, replan_student

    try:
        plan = replan_student(
            REPLAN_DB_PATH,
            student_id,
            start_term=start.strip().upper(),
            max_terms=terms,
            credits_per_term=credits,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if as_markdown:
        return Response(
            content=render_replan_markdown(plan),
            media_type="text/markdown; charset=utf-8",
        )
    return plan


# ─────────────────────────────────────────────────────────────────
# Faculty
# ─────────────────────────────────────────────────────────────────


@router.get("/faculty", response_model=list[FacultyInfo])
async def list_faculty(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """List all faculty members."""
    faculty = session.exec(select(Faculty).offset(skip).limit(limit)).all()
    return [
        FacultyInfo(
            id=f.id,
            name=f.name,
            email=f.email,
            max_credits=f.max_credits,
            approved_courses=f.approved_courses,
            unavailable_slots=f.unavailable_slots,
            preferences=f.preferences,
        )
        for f in faculty
    ]


@router.get("/faculty/{faculty_id}", response_model=FacultyInfo)
async def get_faculty(faculty_id: str, session: Session = Depends(get_session)):
    """Get a specific faculty member."""
    faculty = session.get(Faculty, faculty_id)
    if not faculty:
        raise HTTPException(status_code=404, detail="Faculty not found")
    return FacultyInfo(
        id=faculty.id,
        name=faculty.name,
        email=faculty.email,
        max_credits=faculty.max_credits,
        approved_courses=faculty.approved_courses,
        unavailable_slots=faculty.unavailable_slots,
        preferences=faculty.preferences,
    )


@router.patch("/faculty/{faculty_id}/preferences", response_model=FacultyInfo)
async def update_faculty_preferences(
    faculty_id: str,
    body: FacultyPreferencesUpdate,
    session: Session = Depends(get_session),
):
    """Update faculty scheduling preferences."""
    faculty = session.get(Faculty, faculty_id)
    if not faculty:
        raise HTTPException(status_code=404, detail="Faculty not found")

    if body.unavailable_slots is not None:
        # Validate slot IDs exist
        for slot_id in body.unavailable_slots:
            slot = session.get(TimeSlot, slot_id)
            if not slot:
                raise HTTPException(
                    status_code=400,
                    detail=f"TimeSlot {slot_id} not found",
                )
        faculty.unavailable_slots = body.unavailable_slots

    if body.preferred_slots is not None:
        # Validate slot IDs exist
        for slot_id in body.preferred_slots:
            slot = session.get(TimeSlot, slot_id)
            if not slot:
                raise HTTPException(
                    status_code=400,
                    detail=f"TimeSlot {slot_id} not found",
                )
        prefs = dict(faculty.preferences) if faculty.preferences else {}
        prefs["preferred_slots"] = body.preferred_slots
        faculty.preferences = prefs

    if body.allow_back_to_back is not None:
        prefs = dict(faculty.preferences) if faculty.preferences else {}
        prefs["allow_back_to_back"] = body.allow_back_to_back
        faculty.preferences = prefs

    session.add(faculty)
    session.commit()
    session.refresh(faculty)

    return FacultyInfo(
        id=faculty.id,
        name=faculty.name,
        email=faculty.email,
        max_credits=faculty.max_credits,
        approved_courses=faculty.approved_courses,
        unavailable_slots=faculty.unavailable_slots,
        preferences=faculty.preferences,
    )


# ─────────────────────────────────────────────────────────────────
# Rooms
# ─────────────────────────────────────────────────────────────────


@router.get("/rooms", response_model=list[RoomInfo])
async def list_rooms(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    room_type: str | None = None,
    session: Session = Depends(get_session),
):
    """List all rooms with optional filtering."""
    stmt = select(Room)
    if room_type:
        stmt = stmt.where(Room.room_type == room_type)
    stmt = stmt.offset(skip).limit(limit)

    rooms = session.exec(stmt).all()
    return [
        RoomInfo(
            id=r.id,
            building=r.building,
            number=r.number,
            capacity=r.capacity,
            room_type=r.room_type.value,
        )
        for r in rooms
    ]


class TimeSlotInfo(BaseModel):
    id: int
    name: str
    pattern: str
    days: list[str]
    start_time: str
    end_time: str
    duration_minutes: int
    slot_type: str


@router.get("/timeslots", response_model=list[TimeSlotInfo])
async def list_timeslots(session: Session = Depends(get_session)):
    """List all time slots (days + start/end) for calendar rendering."""
    slots = session.exec(select(TimeSlot)).all()
    return [
        TimeSlotInfo(
            id=t.id,
            name=t.name,
            pattern=t.pattern,
            days=t.days,
            start_time=t.start_time,
            end_time=t.end_time,
            duration_minutes=t.duration_minutes,
            slot_type=t.slot_type,
        )
        for t in slots
    ]


# ─────────────────────────────────────────────────────────────────
# Demands
# ─────────────────────────────────────────────────────────────────


