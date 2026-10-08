"""Freshmen enrollment projection API routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from src.api.schemas import (
    ProjectionCreateRequest,
    ProjectionInfo,
)
from src.database.connection import get_session
from src.database.models import DegreeRequirement, FreshmenProjection

router = APIRouter(prefix="/projections", tags=["projections"])


@router.get("/", response_model=list[ProjectionInfo])
async def list_projections(session: Session = Depends(get_session)):
    """List all freshmen enrollment projections."""
    projections = session.exec(select(FreshmenProjection)).all()
    return [
        ProjectionInfo(
            id=p.id,
            major=p.major,
            estimated_count=p.estimated_count,
            catalog_year=p.catalog_year,
            semester=p.semester,
            is_active=p.is_active,
        )
        for p in projections
    ]


@router.post("/", response_model=ProjectionInfo)
async def create_projection(
    request: ProjectionCreateRequest,
    session: Session = Depends(get_session),
):
    """Create or update a freshmen projection for a major.

    If an active projection already exists for the same major and catalog_year,
    it will be updated.
    """
    if request.estimated_count <= 0:
        raise HTTPException(
            status_code=400, detail="estimated_count must be positive"
        )

    # Validate major has degree requirements
    existing_req = session.exec(
        select(DegreeRequirement).where(
            DegreeRequirement.major == request.major,
            DegreeRequirement.catalog_year == request.catalog_year,
        )
    ).first()
    if not existing_req:
        raise HTTPException(
            status_code=400,
            detail=f"No degree requirements for major={request.major}, "
            f"catalog_year={request.catalog_year}",
        )

    # Check for existing projection with same major + catalog_year
    existing = session.exec(
        select(FreshmenProjection).where(
            FreshmenProjection.major == request.major,
            FreshmenProjection.catalog_year == request.catalog_year,
        )
    ).first()

    if existing:
        existing.estimated_count = request.estimated_count
        existing.semester = request.semester
        existing.is_active = True
        session.add(existing)
        session.commit()
        session.refresh(existing)
        projection = existing
    else:
        projection = FreshmenProjection(
            major=request.major,
            estimated_count=request.estimated_count,
            catalog_year=request.catalog_year,
            semester=request.semester,
            is_active=True,
        )
        session.add(projection)
        session.commit()
        session.refresh(projection)

    return ProjectionInfo(
        id=projection.id,
        major=projection.major,
        estimated_count=projection.estimated_count,
        catalog_year=projection.catalog_year,
        semester=projection.semester,
        is_active=projection.is_active,
    )


@router.delete("/{projection_id}")
async def delete_projection(
    projection_id: int, session: Session = Depends(get_session)
):
    """Delete a freshmen projection."""
    projection = session.get(FreshmenProjection, projection_id)
    if not projection:
        raise HTTPException(status_code=404, detail="Projection not found")

    session.delete(projection)
    session.commit()
    return {"message": f"Projection {projection_id} deleted"}
