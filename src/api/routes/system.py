"""System status routes (optimization lock, health)."""

from fastapi import APIRouter

from src.core import job_lock

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/optimization-status")
async def optimization_status():
    """Whether an optimization is currently running (single-user guard).

    Returns `{"running": false}` when idle, or
    `{"running": true, "kind": ..., "label": ..., "started_at": ...}` while a
    generate / re-solve / finals run is in progress. The web UI polls this to
    disable the optimize controls so two users can't launch solves at once.
    """
    return job_lock.status()
