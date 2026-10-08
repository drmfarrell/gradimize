"""Schedule downloads: Banner CSV, enrollments CSV, iCal, student sheet."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session

from src.core import advisor
from src.core.export import BannerExporter
from src.core.ical import ics_for_instructor, ics_for_room, ics_for_student
from src.database.connection import get_session
from src.database.models import (
    ScheduleVersion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedule", tags=["schedule"])


def _csv_download(csv_content: str, filename: str) -> Response:
    """CSV as a real file download (same pattern as the finals export)."""
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{schedule_id}/export/banner")
def export_schedule_banner(
    schedule_id: int, session: Session = Depends(get_session)
):
    """Export schedule as a Banner-format CSV download."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    exporter = BannerExporter(session)
    csv_content = exporter.export_schedule(schedule_id)
    return _csv_download(csv_content, f"schedule_{schedule_id}_banner.csv")


@router.get("/{schedule_id}/export/enrollments")
def export_enrollments(schedule_id: int, session: Session = Depends(get_session)):
    """Export enrollments as a CSV download."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    exporter = BannerExporter(session)
    csv_content = exporter.export_enrollments(schedule_id)
    return _csv_download(csv_content, f"schedule_{schedule_id}_enrollments.csv")


def _ics_download(text: str, filename: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return Response(
        content=text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@router.get("/{schedule_id}/ical/instructor/{faculty_id}")
def ical_instructor(
    schedule_id: int, faculty_id: str, session: Session = Depends(get_session)
):
    """Weekly .ics calendar of one instructor's meetings in this schedule.

    Imports into Outlook/Google/Apple Calendar. Term dates are approximated
    from the semester code (see src/core/ical.default_term_dates).
    """
    try:
        text = ics_for_instructor(session, schedule_id, faculty_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _ics_download(text, f"schedule{schedule_id}_{faculty_id}.ics")


@router.get("/{schedule_id}/ical/room/{room_id}")
def ical_room(
    schedule_id: int, room_id: str, session: Session = Depends(get_session)
):
    """Weekly .ics calendar of everything meeting in one room."""
    try:
        text = ics_for_room(session, schedule_id, room_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _ics_download(text, f"schedule{schedule_id}_room_{room_id}.ics")


@router.get("/{schedule_id}/ical/student/{student_id}")
def ical_student(
    schedule_id: int, student_id: str, session: Session = Depends(get_session)
):
    """Weekly .ics calendar of one student's enrolled sections."""
    try:
        text = ics_for_student(session, schedule_id, student_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _ics_download(text, f"schedule{schedule_id}_{student_id}.ics")


@router.get("/{schedule_id}/student/{student_id}/sheet")
def student_schedule_sheet(
    schedule_id: int, student_id: str, session: Session = Depends(get_session)
):
    """Printable markdown schedule sheet for one student (advising handout)."""
    try:
        text = advisor.student_sheet(session, schedule_id, student_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'inline; filename="sheet_{schedule_id}_'
                + re.sub(r"[^A-Za-z0-9._-]", "_", student_id)
                + '.md"'
            )
        },
    )
