"""SSE streaming endpoints: generate, re-solve, and repair."""

import logging

from fastapi import APIRouter, HTTPException, Query

from src.api.sse import sse_response
from src.core import job_lock
from src.core.generate import run_generation, run_repair, run_resolve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedule", tags=["schedule"])


# NOTE: a GET that mutates (starts a solver run) — required because
# EventSource can only GET. Token-gated, so not CSRF-able today; revisit if
# per-user auth lands (a pasted/shared URL would trigger a solve).
@router.get("/generate/stream")
async def generate_schedule_stream(
    semester: str = Query("2025SP"),
    name: str = Query("Generated Schedule"),
    workers: int = Query(0, ge=0),
    time_limit: int = Query(120, ge=5, le=1800),
    include_projections: bool = Query(False),
    class_size_cap: int | None = Query(None, ge=1, le=500),
    capacity_mode: str | None = Query(None, pattern="^(fixed|room)$"),
    conflict_threshold: float | None = Query(None, ge=0, le=100000),
):
    """Generate a schedule, streaming live solver progress via SSE.

    Emits `data:` frames carrying JSON objects of the form
    {"type": "log", "msg": ...}, ending with one terminal frame
    {"type": "done", "schedule_id": ..., ...} or {"type": "error", ...}.
    Heartbeat comments (`: ping`) keep the connection alive during the
    solver's long quiet phase.

    When `include_projections` is true, projected freshman-class demand is
    folded into the solve (see run_generation).
    """
    if not job_lock.try_acquire("generate", name):
        raise HTTPException(status_code=409, detail=job_lock.BUSY_MESSAGE)

    def work(emit):
        try:
            run_generation(
                emit, semester, name, workers, time_limit, include_projections,
                class_size_cap=class_size_cap, capacity_mode=capacity_mode,
                conflict_threshold=conflict_threshold,
            )
        finally:
            job_lock.release()

    return sse_response(work)


# NOTE: mutating GET, same EventSource constraint as /generate/stream above.
@router.get("/{schedule_id}/re-solve/stream")
async def re_solve_schedule_stream(
    schedule_id: int,
    name: str | None = Query(None),
    workers: int = Query(0, ge=0),
    time_limit: int = Query(120, ge=5, le=1800),
):
    """Re-solve a schedule, streaming live solver progress via SSE.

    Same event protocol as /generate/stream. Pinned sections keep their
    placement; the rest is re-optimized into a new ScheduleVersion.
    """
    if not job_lock.try_acquire("re-solve", name or f"schedule {schedule_id}"):
        raise HTTPException(status_code=409, detail=job_lock.BUSY_MESSAGE)

    def work(emit):
        try:
            run_resolve(emit, schedule_id, name, workers, time_limit)
        finally:
            job_lock.release()

    return sse_response(work)


# NOTE: mutating GET, same EventSource constraint as /generate/stream above.
@router.get("/{schedule_id}/repair/stream")
async def repair_schedule_stream(
    schedule_id: int,
    name: str | None = Query(None),
    workers: int = Query(0, ge=0),
    time_limit: int = Query(120, ge=5, le=1800),
):
    """Minimal-change repair of a published schedule, streaming via SSE.

    Unlike re-solve (which re-optimizes everything unpinned), repair anchors
    every section to its published slot/room/instructor/roster and only moves
    what current data forces (a resigned instructor, a lost room). The done
    event carries `repair_diff` — what moved vs the base schedule.
    """
    if not job_lock.try_acquire("repair", name or f"schedule {schedule_id}"):
        raise HTTPException(status_code=409, detail=job_lock.BUSY_MESSAGE)

    def work(emit):
        try:
            run_repair(emit, schedule_id, name, workers, time_limit)
        finally:
            job_lock.release()

    return sse_response(work)
