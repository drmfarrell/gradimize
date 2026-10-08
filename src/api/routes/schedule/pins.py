"""Section pin / unpin (manual placement locks)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from src.api.schemas import (
    SectionPinRequest,
    SectionPinResponse,
)
from src.database.connection import get_session
from src.database.models import (
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionMeeting,
    TimeSlot,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedule", tags=["schedule"])


@router.patch("/{schedule_id}/sections/{section_id}/pin", response_model=SectionPinResponse)
async def pin_section(
    schedule_id: int,
    section_id: int,
    request: SectionPinRequest,
    session: Session = Depends(get_session),
):
    """Pin a section to specific time slot, room, and/or faculty."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    section = session.get(Section, section_id)
    if not section or section.schedule_id != schedule_id:
        raise HTTPException(status_code=404, detail="Section not found in this schedule")

    # Validate time_slot if provided
    if request.time_slot_id is not None:
        slot = session.get(TimeSlot, request.time_slot_id)
        if not slot:
            raise HTTPException(
                status_code=400, detail=f"Time slot {request.time_slot_id} not found"
            )

    # Validate room if provided
    if request.room_id is not None:
        room = session.get(Room, request.room_id)
        if not room:
            raise HTTPException(status_code=400, detail=f"Room {request.room_id} not found")

    # Validate faculty if provided
    if request.faculty_id is not None:
        fac = session.get(Faculty, request.faculty_id)
        if not fac:
            raise HTTPException(status_code=400, detail=f"Faculty {request.faculty_id} not found")
        section.faculty_id = request.faculty_id

    section.is_pinned = True
    section.pin_reason = request.pin_reason

    # Handle multi-meeting pinning
    if request.meetings:
        for mp in request.meetings:
            if mp.time_slot_id is not None:
                ts = session.get(TimeSlot, mp.time_slot_id)
                if not ts:
                    raise HTTPException(
                        status_code=400, detail=f"Time slot {mp.time_slot_id} not found"
                    )
            if mp.room_id is not None:
                rm = session.get(Room, mp.room_id)
                if not rm:
                    raise HTTPException(status_code=400, detail=f"Room {mp.room_id} not found")

        # Delete existing SectionMeeting records for this section
        existing = list(
            session.exec(
                select(SectionMeeting).where(SectionMeeting.section_id == section.id)
            ).all()
        )
        for e in existing:
            session.delete(e)

        # Create new SectionMeeting records
        for mp in request.meetings:
            if mp.time_slot_id is not None and mp.room_id is not None:
                sm = SectionMeeting(
                    section_id=section.id,
                    meeting_sequence=mp.meeting_sequence,
                    time_slot_id=mp.time_slot_id,
                    room_id=mp.room_id,
                )
                session.add(sm)
    elif request.time_slot_id is not None or request.room_id is not None:
        # Simple pin: create/update SectionMeeting for meeting_sequence=0.
        # A partial request (only slot or only room) defaults the missing
        # half from the existing meeting-0 record; it must NOT silently
        # no-op — that used to leave drag-moves unapplied but still pinned.
        existing_m0 = session.exec(
            select(SectionMeeting)
            .where(SectionMeeting.section_id == section.id, SectionMeeting.meeting_sequence == 0)
        ).first()

        slot_id = request.time_slot_id
        room_id = request.room_id
        if slot_id is None:
            slot_id = existing_m0.time_slot_id if existing_m0 else None
        if room_id is None:
            room_id = existing_m0.room_id if existing_m0 else None
        if slot_id is None or room_id is None:
            missing = "time_slot_id" if slot_id is None else "room_id"
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Section has no existing meeting to default {missing} from; "
                    "provide both time_slot_id and room_id."
                ),
            )

        if existing_m0:
            existing_m0.time_slot_id = slot_id
            existing_m0.room_id = room_id
            session.add(existing_m0)
        else:
            sm = SectionMeeting(
                section_id=section.id,
                meeting_sequence=0,
                time_slot_id=slot_id,
                room_id=room_id,
            )
            session.add(sm)

    session.add(section)
    session.commit()
    session.refresh(section)

    # Look up slot/room from meeting 0 for response
    m0 = session.exec(
        select(SectionMeeting)
        .where(SectionMeeting.section_id == section.id, SectionMeeting.meeting_sequence == 0)
    ).first()
    slot = session.get(TimeSlot, m0.time_slot_id) if m0 else None
    fac = session.get(Faculty, section.faculty_id) if section.faculty_id else None

    return SectionPinResponse(
        section_id=section.id,
        crn=section.crn,
        is_pinned=True,
        time_slot_name=slot.name if slot else None,
        room_id=m0.room_id if m0 else None,
        faculty_name=fac.name if fac else None,
        pin_reason=section.pin_reason,
    )


@router.delete("/{schedule_id}/sections/{section_id}/pin", response_model=SectionPinResponse)
async def unpin_section(
    schedule_id: int,
    section_id: int,
    session: Session = Depends(get_session),
):
    """Unpin a section, allowing the solver to reassign it on re-solve."""
    schedule = session.get(ScheduleVersion, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    section = session.get(Section, section_id)
    if not section or section.schedule_id != schedule_id:
        raise HTTPException(status_code=404, detail="Section not found in this schedule")

    section.is_pinned = False
    section.pin_reason = None

    session.add(section)
    session.commit()
    session.refresh(section)

    # Look up slot/room from meeting 0 for response
    m0 = session.exec(
        select(SectionMeeting)
        .where(SectionMeeting.section_id == section.id, SectionMeeting.meeting_sequence == 0)
    ).first()
    slot = session.get(TimeSlot, m0.time_slot_id) if m0 else None
    fac = session.get(Faculty, section.faculty_id) if section.faculty_id else None

    return SectionPinResponse(
        section_id=section.id,
        crn=section.crn,
        is_pinned=False,
        time_slot_name=slot.name if slot else None,
        room_id=m0.room_id if m0 else None,
        faculty_name=fac.name if fac else None,
        pin_reason=None,
    )
