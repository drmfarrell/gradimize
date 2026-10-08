"""Conflict-related API routes.

The matrix build is cached in ``src.core.conflicts.get_conflict_matrix_cached``
(shared with the TUI). The endpoint is sync `def` (not async) so FastAPI runs
it in a threadpool: a cold build must not block the event loop, or the whole
server freezes for everyone while one client loads the Conflicts page.
"""

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from src.api.schemas import ConflictMatrixResponse, ConflictPair
from src.core.conflicts import get_conflict_matrix_cached
from src.database.connection import get_session

router = APIRouter(prefix="/conflicts", tags=["conflicts"])


@router.get("/", response_model=ConflictMatrixResponse)
def get_conflict_matrix(
    threshold: float = Query(50.0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """Get high-conflict course pairs."""
    matrix = get_conflict_matrix_cached(session)

    if matrix.matrix is None or matrix.matrix.empty:
        return ConflictMatrixResponse(
            total_courses=0,
            high_conflict_pairs=[],
            threshold=threshold,
        )

    pairs = matrix.get_high_conflict_pairs(threshold)[:limit]

    return ConflictMatrixResponse(
        total_courses=len(matrix.matrix.index),
        high_conflict_pairs=[
            ConflictPair(course1=c1, course2=c2, conflict_score=score)
            for c1, c2, score in pairs
        ],
        threshold=threshold,
    )


