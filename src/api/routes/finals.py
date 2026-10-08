"""Finals scheduling API routes."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, func, select

from src.core import job_lock
from src.core.finals import (
    DAY_LABELS,
    FinalsConfig,
    FinalsEngine,
    FinalsExporter,
)
from src.database.connection import get_session
from src.database.models import (
    FinalExamAssignment,
    FinalExamBlock,
    FinalsSchedule,
    ScheduleVersion,
    Section,
    TimeSlot,
)

router = APIRouter(prefix="/finals", tags=["finals"])

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Request / response schemas
# ─────────────────────────────────────────────────────────────────


class FinalsConfigBody(BaseModel):
    """Subset of FinalsConfig that callers can override.

    All fields optional; omitted fields use FinalsConfig defaults.
    """
    start_time: Optional[str] = None
    block_minutes: Optional[int] = None
    gap_minutes: Optional[int] = None
    blocks_per_day: Optional[int] = None
    exam_days: Optional[list[str]] = None
    overflow_days: Optional[list[str]] = None
    evening_in_place_threshold: Optional[str] = None
    weight_time_of_day_per_minute: Optional[int] = None
    weight_day_mismatch: Optional[int] = None
    weight_overflow_day: Optional[int] = None
    weight_two_exams_per_day: Optional[int] = None
    weight_three_exams_per_day: Optional[int] = None
    weight_level_prior: Optional[int] = None
    max_time_seconds: Optional[int] = None
    num_workers: Optional[int] = None


class FinalsSolveRequest(BaseModel):
    name: str = "Finals"
    config: Optional[FinalsConfigBody] = None


class FinalsSolveResponse(BaseModel):
    finals_schedule_id: int
    status: str
    objective_value: float
    solve_time_seconds: float
    num_solver_sections: int
    num_in_place_sections: int
    num_skipped_sections: int
    num_students_with_conflict: int = 0
    num_block_conflicts: int = 0


class FinalsBlockOut(BaseModel):
    id: int
    day: str
    day_label: str
    ordinal: int
    start_time: str
    end_time: str
    label: str


class FinalsAssignmentOut(BaseModel):
    section_id: int
    crn: str
    course_code: str
    section_number: str
    block_id: Optional[int]
    block_label: Optional[str]
    room_id: str
    is_in_place: bool
    in_place_time_slot_name: Optional[str] = None


class FinalsScheduleDetail(BaseModel):
    id: int
    schedule_id: int
    name: str
    created_at: str
    status: str
    config: dict
    solver_stats: dict
    blocks: list[FinalsBlockOut]
    assignments: list[FinalsAssignmentOut]


class FinalsScheduleSummary(BaseModel):
    id: int
    schedule_id: int
    name: str
    created_at: str
    status: str
    num_assignments: int


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _build_config(body: Optional[FinalsConfigBody]) -> FinalsConfig:
    cfg = FinalsConfig()
    if body is None:
        return cfg
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(cfg, field, value)
    return cfg


def _serialize_finals_schedule(
    session: Session, fs: FinalsSchedule
) -> FinalsScheduleDetail:
    blocks = list(session.exec(
        select(FinalExamBlock).where(
            FinalExamBlock.finals_schedule_id == fs.id
        )
    ).all())
    block_outs = [
        FinalsBlockOut(
            id=b.id, day=b.day, day_label=DAY_LABELS.get(b.day, b.day),
            ordinal=b.ordinal, start_time=b.start_time,
            end_time=b.end_time, label=b.label,
        )
        for b in sorted(blocks, key=lambda b: (b.day, b.ordinal))
    ]
    block_label_by_id = {b.id: b.label for b in blocks}

    assignments = list(session.exec(
        select(FinalExamAssignment).where(
            FinalExamAssignment.finals_schedule_id == fs.id
        )
    ).all())
    sec_ids = [a.section_id for a in assignments]
    sections = list(session.exec(
        select(Section).where(Section.id.in_(sec_ids))
    ).all()) if sec_ids else []
    sec_by_id = {s.id: s for s in sections}
    slot_ids = [a.in_place_time_slot_id for a in assignments if a.in_place_time_slot_id]
    slots = {
        s.id: s for s in session.exec(
            select(TimeSlot).where(TimeSlot.id.in_(slot_ids))
        ).all()
    } if slot_ids else {}

    assignment_outs = []
    for a in assignments:
        sec = sec_by_id.get(a.section_id)
        if not sec:
            continue
        in_place_label = None
        if a.is_in_place and a.in_place_time_slot_id is not None:
            ts = slots.get(a.in_place_time_slot_id)
            if ts:
                in_place_label = ts.name
        assignment_outs.append(FinalsAssignmentOut(
            section_id=a.section_id,
            crn=sec.crn,
            course_code=sec.course_code,
            section_number=sec.section_number,
            block_id=a.block_id,
            block_label=block_label_by_id.get(a.block_id) if a.block_id else None,
            room_id=a.room_id,
            is_in_place=a.is_in_place,
            in_place_time_slot_name=in_place_label,
        ))

    return FinalsScheduleDetail(
        id=fs.id, schedule_id=fs.schedule_id, name=fs.name,
        created_at=fs.created_at.isoformat(), status=fs.status,
        config=fs.config or {}, solver_stats=fs.solver_stats or {},
        blocks=block_outs, assignments=assignment_outs,
    )


# ─────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────


# NOTE: sync `def` (not async) so FastAPI runs this CPU-heavy solve in a
# threadpool. If it were `async def`, the blocking CP-SAT call would freeze the
# event loop for the whole solve, making the server unresponsive to everyone
# (including the optimization-status poll) until it finished.
@router.post("/{schedule_id}/solve", response_model=FinalsSolveResponse)
def solve_finals(
    schedule_id: int,
    request: FinalsSolveRequest,
    session: Session = Depends(get_session),
):
    """Solve finals for a given semester schedule.

    Creates a new FinalsSchedule with assignments. Multiple finals schedules
    can exist per semester schedule (e.g., for different config experiments).
    """
    if not session.get(ScheduleVersion, schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    cfg = _build_config(request.config)
    if not job_lock.try_acquire("finals", request.name):
        raise HTTPException(status_code=409, detail=job_lock.BUSY_MESSAGE)
    try:
        engine = FinalsEngine(session, schedule_id, cfg)
        result = engine.solve(name=request.name)
    except Exception as e:
        logger.exception("Unhandled error in %s", __name__)
        raise HTTPException(status_code=500, detail="Internal server error") from e
    finally:
        job_lock.release()
    if result.status not in ("optimal", "feasible"):
        raise HTTPException(
            status_code=400, detail=f"Solver returned status: {result.status}"
        )
    stats = result.finals_schedule.solver_stats
    return FinalsSolveResponse(
        finals_schedule_id=result.finals_schedule.id,
        status=result.status,
        objective_value=result.objective_value,
        solve_time_seconds=result.solve_time_seconds,
        num_solver_sections=stats.get("num_solver_sections", 0),
        num_in_place_sections=stats.get("num_in_place_sections", 0),
        num_skipped_sections=stats.get("num_skipped_sections", 0),
        num_students_with_conflict=stats.get("num_students_with_conflict", 0),
        num_block_conflicts=stats.get("num_block_conflicts", 0),
    )


@router.get(
    "/by-schedule/{schedule_id}", response_model=list[FinalsScheduleSummary]
)
def list_finals_for_schedule(
    schedule_id: int, session: Session = Depends(get_session)
):
    """List all finals schedules belonging to a semester schedule."""
    fs_list = list(session.exec(
        select(FinalsSchedule).where(FinalsSchedule.schedule_id == schedule_id)
    ).all())
    counts = dict(session.exec(
        select(FinalExamAssignment.finals_schedule_id, func.count())
        .group_by(FinalExamAssignment.finals_schedule_id)
    ).all())
    return [
        FinalsScheduleSummary(
            id=fs.id, schedule_id=fs.schedule_id, name=fs.name,
            created_at=fs.created_at.isoformat(), status=fs.status,
            num_assignments=counts.get(fs.id, 0),
        )
        for fs in fs_list
    ]


@router.get("/{finals_schedule_id}", response_model=FinalsScheduleDetail)
async def get_finals_schedule(
    finals_schedule_id: int, session: Session = Depends(get_session)
):
    """Fetch a finals schedule with its blocks and assignments."""
    fs = session.get(FinalsSchedule, finals_schedule_id)
    if not fs:
        raise HTTPException(status_code=404, detail="Finals schedule not found")
    return _serialize_finals_schedule(session, fs)


@router.delete("/{finals_schedule_id}")
async def delete_finals_schedule(
    finals_schedule_id: int, session: Session = Depends(get_session)
):
    fs = session.get(FinalsSchedule, finals_schedule_id)
    if not fs:
        raise HTTPException(status_code=404, detail="Finals schedule not found")
    for a in session.exec(
        select(FinalExamAssignment).where(
            FinalExamAssignment.finals_schedule_id == finals_schedule_id
        )
    ).all():
        session.delete(a)
    for b in session.exec(
        select(FinalExamBlock).where(
            FinalExamBlock.finals_schedule_id == finals_schedule_id
        )
    ).all():
        session.delete(b)
    session.delete(fs)
    session.commit()
    return {"message": f"Finals schedule {finals_schedule_id} deleted"}


@router.get("/{finals_schedule_id}/export.csv")
async def export_finals_csv(
    finals_schedule_id: int, session: Session = Depends(get_session)
):
    if not session.get(FinalsSchedule, finals_schedule_id):
        raise HTTPException(status_code=404, detail="Finals schedule not found")
    csv_content = FinalsExporter(session).export_csv(finals_schedule_id)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="finals_{finals_schedule_id}.csv"'
            )
        },
    )


