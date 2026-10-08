"""Schedule CRUD: import, list, detail, delete."""

import logging
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete as sa_delete
from sqlmodel import Session, func, select

from src.api.schemas import (
    MeetingDetail,
    ScheduleDetail,
    ScheduleImportRequest,
    ScheduleImportResponse,
    ScheduleSummary,
    SectionDetail,
)
from src.core.ingest import DataIngestor
from src.database.connection import get_session
from src.database.models import (
    Faculty,
    FinalExamAssignment,
    FinalExamBlock,
    FinalsSchedule,
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedule", tags=["schedule"])


@router.post("/import", response_model=ScheduleImportResponse)
def import_schedule(
    request: ScheduleImportRequest, session: Session = Depends(get_session)
):
    """Import a Banner CSV schedule into a pinned ScheduleVersion."""
    # 50 MB cap: uvicorn has no default body limit, so an unchecked string
    # field would buffer arbitrarily large requests in RAM.
    if len(request.csv_content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="csv_content exceeds 50 MB limit")
    try:
        from src.core.runlog import RunLog

        with RunLog("import") as runlog:
            ingestor = DataIngestor(session, ".", runlog=runlog)
            import_result = ingestor.import_banner_schedule(
                csv_content=request.csv_content,
                schedule_name=request.schedule_name,
                semester=request.semester,
            )
        return ScheduleImportResponse(
            schedule_id=import_result.schedule_id,
            sections_imported=import_result.sections_imported,
            meetings_imported=import_result.meetings_imported,
            warnings=import_result.warnings,
            errors=import_result.errors,
        )
    except Exception as e:
        logger.exception("Unhandled error in %s", __name__)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get("/", response_model=list[ScheduleSummary])
def list_schedules(session: Session = Depends(get_session)):
    """List all schedules.

    Polled by several screens — one grouped COUNT instead of loading every
    Section row per schedule.
    """
    schedules = session.exec(select(ScheduleVersion)).all()
    counts = dict(
        session.exec(
            select(Section.schedule_id, func.count()).group_by(Section.schedule_id)
        ).all()
    )
    return [
        ScheduleSummary(
            id=schedule.id,
            name=schedule.name,
            semester=schedule.semester,
            status=schedule.status,
            created_at=schedule.created_at.isoformat(),
            sections_count=counts.get(schedule.id, 0),
        )
        for schedule in schedules
    ]


@router.get("/{schedule_id}", response_model=ScheduleDetail)
def get_schedule(schedule_id: int, session: Session = Depends(get_session)):
    """Get detailed schedule information."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Get sections with related data
    sections = list(
        session.exec(select(Section).where(Section.schedule_id == schedule_id)).all()
    )
    section_ids = [s.id for s in sections if s.id is not None]

    # Load lookup tables
    time_slots = {t.id: t for t in session.exec(select(TimeSlot)).all()}
    rooms = {r.id: r for r in session.exec(select(Room)).all()}
    faculty = {f.id: f for f in session.exec(select(Faculty)).all()}

    # Hottest endpoint (re-fetched after every pin/edit/drag): bulk-fetch
    # enrollment counts and meetings instead of two queries per section.
    enrollment_counts: dict[int, int] = {}
    meetings_by_section: dict[int, list[SectionMeeting]] = defaultdict(list)
    if section_ids:
        enrollment_counts = dict(
            session.exec(
                select(SectionEnrollment.section_id, func.count())
                .where(SectionEnrollment.section_id.in_(section_ids))
                .group_by(SectionEnrollment.section_id)
            ).all()
        )
        for sm in session.exec(
            select(SectionMeeting)
            .where(SectionMeeting.section_id.in_(section_ids))
            .order_by(SectionMeeting.section_id, SectionMeeting.meeting_sequence)  # type: ignore
        ).all():
            meetings_by_section[sm.section_id].append(sm)

    section_details = []
    for s in sections:
        fac = faculty.get(s.faculty_id) if s.faculty_id else None
        enrollment_count = enrollment_counts.get(s.id, 0)

        meeting_details = []
        if s.id is not None:
            for sm in meetings_by_section.get(s.id, []):
                m_slot = time_slots.get(sm.time_slot_id)
                m_room = rooms.get(sm.room_id)
                meeting_details.append(
                    MeetingDetail(
                        meeting_sequence=sm.meeting_sequence,
                        time_slot_id=sm.time_slot_id,
                        time_slot_name=m_slot.name if m_slot else "Unknown",
                        room_id=sm.room_id,
                        room_name=f"{m_room.building} {m_room.number}" if m_room else "Unknown",
                    )
                )

        # Derive section-level slot/room from meeting 0
        slot = None
        room = None
        if meeting_details:
            slot = time_slots.get(meeting_details[0].time_slot_id)
            room = rooms.get(meeting_details[0].room_id)

        section_details.append(
            SectionDetail(
                id=s.id,
                crn=s.crn,
                course_code=s.course_code,
                section_number=s.section_number,
                capacity=s.capacity,
                time_slot_name=slot.name if slot else None,
                time_slot_id=meeting_details[0].time_slot_id if meeting_details else None,
                room_id=room.id if room else None,
                room_name=f"{room.building} {room.number}" if room else None,
                faculty_name=fac.name if fac else None,
                faculty_id=s.faculty_id,
                enrollment_count=enrollment_count,
                is_pinned=s.is_pinned,
                pin_reason=s.pin_reason,
                meetings=meeting_details,
            )
        )

    return ScheduleDetail(
        id=schedule.id,
        name=schedule.name,
        semester=schedule.semester,
        status=schedule.status,
        created_at=schedule.created_at.isoformat(),
        solver_stats=schedule.solver_stats,
        sections=section_details,
    )


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: int, session: Session = Depends(get_session)):
    """Delete a schedule and everything hanging off it.

    SQLite FK enforcement is off and the models define no cascades, so the
    cascade is manual — and must include the finals tables, which reference
    both the schedule and its sections. Sync def: thousands of deletes must
    not run on the event loop.
    """
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Finals schedules built on this schedule (assignments + blocks + header)
    for fs in session.exec(
        select(FinalsSchedule).where(FinalsSchedule.schedule_id == schedule_id)
    ).all():
        session.execute(
            sa_delete(FinalExamAssignment).where(
                FinalExamAssignment.finals_schedule_id == fs.id
            )
        )
        session.execute(
            sa_delete(FinalExamBlock).where(
                FinalExamBlock.finals_schedule_id == fs.id
            )
        )
        session.delete(fs)

    # Meetings and enrollments for this schedule's sections, then the sections
    section_ids = [
        s.id
        for s in session.exec(
            select(Section).where(Section.schedule_id == schedule_id)
        ).all()
        if s.id is not None
    ]
    if section_ids:
        session.execute(
            sa_delete(SectionMeeting).where(SectionMeeting.section_id.in_(section_ids))
        )
        session.execute(
            sa_delete(SectionEnrollment).where(
                SectionEnrollment.section_id.in_(section_ids)
            )
        )
        session.execute(sa_delete(Section).where(Section.schedule_id == schedule_id))

    session.delete(schedule)
    session.commit()

    return {"message": f"Schedule {schedule_id} deleted"}
