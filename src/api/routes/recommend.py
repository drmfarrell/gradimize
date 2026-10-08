"""Recommendation API routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from src.api.schemas import RecommendationItem, RecommendationResponse
from src.core.recommend import analyze_schedule
from src.database.connection import get_session
from src.database.models import ScheduleVersion

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

logger = logging.getLogger(__name__)



@router.get("/{schedule_id}", response_model=RecommendationResponse)
def get_recommendations(
    schedule_id: int,
    conflict_threshold: float = 50.0,
    session: Session = Depends(get_session),
):
    """Get improvement recommendations for a schedule."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    try:
        recs = analyze_schedule(session, schedule_id, conflict_threshold)
        items = [
            RecommendationItem(
                type=r.type.value,
                priority=r.priority,
                course_code=r.course_code,
                section_crn=r.section_crn,
                description=r.description,
                suggested_action=r.suggested_action,
            )
            for r in recs
        ]
        return RecommendationResponse(
            schedule_id=schedule_id,
            total_recommendations=len(items),
            recommendations=items,
        )
    except Exception as e:
        logger.exception("Unhandled error in %s", __name__)
        raise HTTPException(status_code=500, detail="Internal server error") from e
