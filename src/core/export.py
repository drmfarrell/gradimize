"""Banner CSV export for Gradimize.

Specifically the Ellucian Banner section/enrollment export (plus room- and
faculty-utilization CSVs). The other two export formats live elsewhere:
``ical.py`` for .ics calendars and ``finals.py`` for the finals-grid export.
"""

import csv
import logging
from datetime import datetime
from io import StringIO
from pathlib import Path

from sqlmodel import Session, func, select

from src.database.models import (
    Course,
    Faculty,
    Room,
    ScheduleVersion,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

logger = logging.getLogger(__name__)


def _split_course_code(code: str) -> tuple[str, str]:
    """Split "BIO101" / "BIOL101" into (subject, number) at the first digit."""
    for i, ch in enumerate(code):
        if ch.isdigit():
            return code[:i], code[i:]
    return code, ""


class BannerExporter:
    """Exports schedules to Banner-compatible CSV format."""

    def __init__(self, session: Session):
        """Initialize exporter.

        Args:
            session: Database session.
        """
        self.session = session

    def export_schedule(
        self, schedule_id: int, output_path: Path | str | None = None
    ) -> str:
        """Export a schedule to Banner CSV format.

        Args:
            schedule_id: ID of schedule to export.
            output_path: Optional path to write CSV file.

        Returns:
            CSV string content.
        """
        schedule = self.session.get(ScheduleVersion, schedule_id)
        if not schedule:
            raise ValueError(f"Schedule {schedule_id} not found")

        sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == schedule_id)
            ).all()
        )

        # Load related data
        courses = {c.code: c for c in self.session.exec(select(Course)).all()}
        rooms = {r.id: r for r in self.session.exec(select(Room)).all()}
        faculty = {f.id: f for f in self.session.exec(select(Faculty)).all()}
        time_slots = {t.id: t for t in self.session.exec(select(TimeSlot)).all()}

        # Banner CSV format
        output = StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow(
            [
                "CRN",
                "SUBJECT",
                "COURSE_NUMBER",
                "SECTION",
                "TITLE",
                "CREDITS",
                "CAMPUS",
                "STATUS",
                "MEETING_DAYS",
                "START_TIME",
                "END_TIME",
                "BUILDING",
                "ROOM",
                "CAPACITY",
                "ENROLLMENT",
                "INSTRUCTOR_ID",
                "INSTRUCTOR_NAME",
                "PART_OF_TERM",
            ]
        )

        # Bulk-load meetings and enrollment counts (one query each, not two
        # per section).
        section_ids = [s.id for s in sections if s.id is not None]
        section_meetings: dict[int, list[SectionMeeting]] = {}
        enrollment_counts: dict[int, int] = {}
        if section_ids:
            for sm in self.session.exec(
                select(SectionMeeting)
                .where(SectionMeeting.section_id.in_(section_ids))
                .order_by(SectionMeeting.section_id, SectionMeeting.meeting_sequence)  # type: ignore
            ).all():
                section_meetings.setdefault(sm.section_id, []).append(sm)
            enrollment_counts = dict(
                self.session.exec(
                    select(SectionEnrollment.section_id, func.count())
                    .where(SectionEnrollment.section_id.in_(section_ids))
                    .group_by(SectionEnrollment.section_id)
                ).all()
            )

        for section in sections:
            course = courses.get(section.course_code)
            instructor = faculty.get(section.faculty_id) if section.faculty_id else None
            enrollment_count = enrollment_counts.get(section.id, 0)

            # Split course code at the first digit ("BIO101" → "BIO", "101");
            # a fixed [:4] slice corrupted 3-letter subjects.
            subject, course_number = _split_course_code(section.course_code)

            def _row(meeting_days: str, start_time: str, end_time: str,
                     building: str, room_number: str) -> list:
                return [
                    section.crn,
                    subject,
                    course_number,
                    section.section_number,
                    course.title if course else "",
                    course.credits if course else "",
                    "MAIN",
                    "A",
                    meeting_days,
                    start_time,
                    end_time,
                    building,
                    room_number,
                    section.capacity,
                    enrollment_count,
                    instructor.id if instructor else "",
                    instructor.name if instructor else "TBA",
                    "1",
                ]

            meetings = section_meetings.get(section.id, [])
            if not meetings:
                # Unscheduled/unmatched sections still appear in the export,
                # with blank meeting columns, instead of silently vanishing.
                writer.writerow(_row("", "", "", "", ""))
                continue

            for sm in meetings:
                slot = time_slots.get(sm.time_slot_id)
                room = rooms.get(sm.room_id)
                writer.writerow(
                    _row(
                        "".join(slot.days) if slot else "",
                        slot.start_time if slot else "",
                        slot.end_time if slot else "",
                        room.building if room else "",
                        room.number if room else "",
                    )
                )

        csv_content = output.getvalue()

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", newline="") as f:
                f.write(csv_content)
            logger.info(f"Exported {len(sections)} sections to {output_path}")

        return csv_content

    def export_enrollments(
        self, schedule_id: int, output_path: Path | str | None = None
    ) -> str:
        """Export student enrollments for a schedule.

        Args:
            schedule_id: ID of schedule to export.
            output_path: Optional path to write CSV file.

        Returns:
            CSV string content.
        """
        sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == schedule_id)
            ).all()
        )
        section_ids = {s.id for s in sections}

        enrollments = list(
            self.session.exec(
                select(SectionEnrollment).where(
                    SectionEnrollment.section_id.in_(section_ids)  # type: ignore
                )
            ).all()
        )

        # Create CRN lookup
        crn_by_section = {s.id: s.crn for s in sections}
        course_by_section = {s.id: s.course_code for s in sections}

        output = StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow(
            [
                "STUDENT_ID",
                "CRN",
                "COURSE_CODE",
                "STATUS",
                "REGISTRATION_DATE",
            ]
        )

        for enrollment in enrollments:
            writer.writerow(
                [
                    enrollment.student_id,
                    crn_by_section.get(enrollment.section_id, ""),
                    course_by_section.get(enrollment.section_id, ""),
                    "RE" if enrollment.is_confirmed else "RW",  # Registered or Wait
                    datetime.now().strftime("%Y-%m-%d"),
                ]
            )

        csv_content = output.getvalue()

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", newline="") as f:
                f.write(csv_content)
            logger.info(f"Exported {len(enrollments)} enrollments to {output_path}")

        return csv_content

    def export_room_utilization(
        self, schedule_id: int, output_path: Path | str | None = None
    ) -> str:
        """Export room utilization report.

        Args:
            schedule_id: ID of schedule to export.
            output_path: Optional path to write CSV file.

        Returns:
            CSV string content.
        """
        sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == schedule_id)
            ).all()
        )
        rooms = list(self.session.exec(select(Room)).all())
        time_slots = list(self.session.exec(select(TimeSlot)).all())

        # Build utilization data from SectionMeeting records
        room_usage: dict[str, list[str]] = {r.id: [] for r in rooms}
        for section in sections:
            if section.id is not None:
                sm_list = list(
                    self.session.exec(
                        select(SectionMeeting)
                        .where(SectionMeeting.section_id == section.id)
                    ).all()
                )
                for sm in sm_list:
                    slot = next((s for s in time_slots if s.id == sm.time_slot_id), None)
                    if slot and sm.room_id in room_usage:
                        room_usage[sm.room_id].append(
                            f"{section.course_code} ({slot.name})"
                        )

        output = StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow(
            [
                "ROOM_ID",
                "BUILDING",
                "ROOM_NUMBER",
                "ROOM_TYPE",
                "CAPACITY",
                "SECTIONS_SCHEDULED",
                "UTILIZATION_PERCENT",
                "COURSES",
            ]
        )

        total_slots = len(time_slots)
        for room in rooms:
            sections_count = len(room_usage.get(room.id, []))
            utilization = (sections_count / total_slots * 100) if total_slots > 0 else 0

            writer.writerow(
                [
                    room.id,
                    room.building,
                    room.number,
                    room.room_type.value,
                    room.capacity,
                    sections_count,
                    f"{utilization:.1f}%",
                    "; ".join(room_usage.get(room.id, [])),
                ]
            )

        csv_content = output.getvalue()

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", newline="") as f:
                f.write(csv_content)
            logger.info(f"Exported room utilization to {output_path}")

        return csv_content

    def export_faculty_schedule(
        self, schedule_id: int, output_path: Path | str | None = None
    ) -> str:
        """Export faculty teaching schedules.

        Args:
            schedule_id: ID of schedule to export.
            output_path: Optional path to write CSV file.

        Returns:
            CSV string content.
        """
        sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == schedule_id)
            ).all()
        )
        faculty_list = list(self.session.exec(select(Faculty)).all())
        courses = {c.code: c for c in self.session.exec(select(Course)).all()}
        time_slots = {t.id: t for t in self.session.exec(select(TimeSlot)).all()}
        rooms = {r.id: r for r in self.session.exec(select(Room)).all()}

        # Group sections by faculty. A section can reference a faculty_id
        # that no longer exists (FKs unenforced; faculty re-imports wipe the
        # table), so unknown IDs are skipped with a warning, not a KeyError.
        by_faculty: dict[str, list[Section]] = {f.id: [] for f in faculty_list}
        for section in sections:
            if not section.faculty_id:
                continue
            if section.faculty_id not in by_faculty:
                logger.warning(
                    "Section %s references unknown faculty %s; omitting from "
                    "faculty export", section.crn, section.faculty_id,
                )
                continue
            by_faculty[section.faculty_id].append(section)

        output = StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow(
            [
                "FACULTY_ID",
                "FACULTY_NAME",
                "EMAIL",
                "TOTAL_CREDITS",
                "MAX_CREDITS",
                "SECTIONS",
                "SCHEDULE_DETAIL",
            ]
        )

        for faculty in faculty_list:
            faculty_sections = by_faculty.get(faculty.id, [])

            total_credits = sum(
                courses[s.course_code].credits
                for s in faculty_sections
                if s.course_code in courses
            )

            schedule_details = []
            for s in faculty_sections:
                # Try SectionMeeting records first
                sm_list = []
                if s.id is not None:
                    sm_list = list(
                        self.session.exec(
                            select(SectionMeeting)
                            .where(SectionMeeting.section_id == s.id)
                            .order_by(SectionMeeting.meeting_sequence)  # type: ignore
                        ).all()
                    )
                for sm in sm_list:
                    slot = time_slots.get(sm.time_slot_id)
                    room = rooms.get(sm.room_id)
                    detail = f"{s.course_code}-{s.section_number}"
                    if slot:
                        detail += f" {slot.name}"
                    if room:
                        detail += f" {room.id}"
                    schedule_details.append(detail)

            writer.writerow(
                [
                    faculty.id,
                    faculty.name,
                    faculty.email,
                    total_credits,
                    faculty.max_credits,
                    len(faculty_sections),
                    "; ".join(schedule_details),
                ]
            )

        csv_content = output.getvalue()

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", newline="") as f:
                f.write(csv_content)
            logger.info(f"Exported faculty schedules to {output_path}")

        return csv_content


    def export_all_csv(
        self, schedule_id: int, output_dir: Path | str
    ) -> dict[str, Path]:
        """Export all schedule data as separate CSV files.

        Args:
            schedule_id: ID of schedule to export.
            output_dir: Directory to write files.

        Returns:
            Dictionary mapping export type to file path.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        exported_files = {}

        schedule_path = output_dir / f"schedule_{schedule_id}.csv"
        self.export_schedule(schedule_id, schedule_path)
        exported_files["schedule"] = schedule_path

        enrollment_path = output_dir / f"enrollments_{schedule_id}.csv"
        self.export_enrollments(schedule_id, enrollment_path)
        exported_files["enrollments"] = enrollment_path

        room_path = output_dir / f"room_utilization_{schedule_id}.csv"
        self.export_room_utilization(schedule_id, room_path)
        exported_files["room_utilization"] = room_path

        faculty_path = output_dir / f"faculty_schedules_{schedule_id}.csv"
        self.export_faculty_schedule(schedule_id, faculty_path)
        exported_files["faculty_schedules"] = faculty_path

        logger.info(f"Exported CSV files for schedule {schedule_id} to {output_dir}")
        return exported_files

    def export_excel(
        self, schedule_id: int, output_path: Path | str
    ) -> Path:
        """Export schedule data as a single Excel workbook with multiple sheets.

        Sheets: Schedule, Enrollments, Room Utilization, Faculty Schedules.

        Args:
            schedule_id: ID of schedule to export.
            output_path: Path for the .xlsx file.

        Returns:
            Path to the written file.
        """
        from openpyxl import Workbook

        from src.core.xlsx_util import write_csv_sheet as _write_csv_to_sheet

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        wb = Workbook()

        # Sheet 1: Schedule
        ws_schedule = wb.active
        ws_schedule.title = "Schedule"
        _write_csv_to_sheet(ws_schedule, self.export_schedule(schedule_id))

        # Sheet 2: Enrollments
        ws_enroll = wb.create_sheet("Enrollments")
        _write_csv_to_sheet(ws_enroll, self.export_enrollments(schedule_id))

        # Sheet 3: Room Utilization
        ws_rooms = wb.create_sheet("Room Utilization")
        _write_csv_to_sheet(ws_rooms, self.export_room_utilization(schedule_id))

        # Sheet 4: Faculty Schedules
        ws_faculty = wb.create_sheet("Faculty Schedules")
        _write_csv_to_sheet(ws_faculty, self.export_faculty_schedule(schedule_id))

        wb.save(output_path)
        logger.info(f"Exported Excel workbook for schedule {schedule_id} to {output_path}")
        return output_path

