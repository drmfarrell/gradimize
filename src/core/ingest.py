"""CSV data ingestion and validation for Gradimize."""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import yaml
from sqlmodel import Session, select

from src.core.runlog import RunLog, describe
from src.database.models import (
    ClassStanding,
    Course,
    DegreeRequirement,
    Faculty,
    FreshmenProjection,
    Prerequisite,
    Room,
    RoomType,
    ScheduleVersion,
    Section,
    SectionMeeting,
    SemesterGuide,
    SemesterGuideEntry,
    StructureType,
    Student,
    StudentCourseHistory,
    TimeSlot,
)

logger = logging.getLogger(__name__)

# Direct-identifier columns that are NEVER stored for students, even if a raw
# (non-deidentified) extract is delivered — the backstop behind the claim in
# docs/DATA_HANDLING_PROCEDURES.md §2/§5. Keep in sync with DROP_COLUMNS in
# scripts/deidentify.py. Faculty name/email are deliberately kept (directory
# data, policy §3).
STUDENT_IDENTIFIER_COLUMNS = {
    "name", "email", "student_name", "first_name", "last_name",
    "middle_name", "preferred_name", "preferred_first_name",
    "phone", "phone_number", "email_address",
    "address", "home_address", "mailing_address",
    "address1", "address2", "street", "city", "state",
    "zip", "zip_code", "postal_code",
    "dob", "date_of_birth", "birth_date", "birthdate", "ssn",
    "netid", "username",
}


class ValidationError(Exception):
    """Raised when data validation fails."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"Validation failed with {len(errors)} errors")


@dataclass
class ImportResult:
    schedule_id: int | None = None
    sections_imported: int = 0
    meetings_imported: int = 0
    crosslist_backfilled: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DataIngestor:
    """Handles CSV data import with validation."""

    def __init__(self, session: Session, data_dir: Path | str,
                 runlog: RunLog | None = None):
        """Initialize ingestor.

        Args:
            session: Database session.
            data_dir: Directory containing CSV files.
            runlog: FERPA-safe run log (see src/core/runlog.py). Defaults to
                a no-op; callers that own a run (uploads, doctor) pass one.
        """
        self.session = session
        self.data_dir = Path(data_dir)
        self.errors: list[str] = []
        self.runlog = runlog or RunLog.disabled()

    def _add_error(self, message: str, kind: str = "import_error", **fields):
        """Record a validation error whose message is FERPA-safe verbatim
        (course codes, room ids, faculty ids — never student row values).
        The message goes to the errors list, the console log, AND the run log.
        """
        self.errors.append(message)
        logger.error(message)
        self.runlog.event(kind, msg=message, **fields)

    def _add_error_student(self, message: str, kind: str, **fields):
        """Record a validation error from a student-data path. The full
        message (with values) goes ONLY to the errors list, i.e. the
        operator's import report. Both log channels get just the structured
        fields — pass value *shapes* via describe(), never values — because
        the app log persists to disk and feeds bug reports (logsetup.py).
        """
        self.errors.append(message)
        logger.error(
            "%s: %s in %s row %s (value shape: %s) — full detail is in the "
            "operator's import report",
            kind, fields.get("column", "field"), fields.get("file", "?"),
            fields.get("row", "?"), fields.get("shape", "?"),
        )
        self.runlog.event(kind, **fields)

    def _read_csv(self, filename: str, sensitive: bool = False) -> pd.DataFrame | None:
        """Read a CSV file from the data directory.

        Args:
            filename: Name of CSV file.
            sensitive: True for student-data files — parse errors then log
                only the exception type to the run log (pandas messages can
                embed cell content).

        Returns:
            DataFrame or None if file doesn't exist.
        """
        filepath = self.data_dir / filename
        if not filepath.exists():
            self._add_error(f"File not found: {filepath}", kind="file_missing",
                            file=filename)
            return None

        try:
            df = pd.read_csv(filepath)
            logger.info(f"Read {len(df)} rows from {filename}")
            return df
        except Exception as e:
            if sensitive:
                self._add_error_student(
                    f"Error reading {filename}: {e}", "read_error",
                    file=filename, exception=type(e).__name__,
                )
            else:
                self._add_error(f"Error reading {filename}: {e}",
                                kind="read_error", file=filename)
            return None

    # ─────────────────────────────────────────────────────────────────
    # Time Slots (from YAML config)
    # ─────────────────────────────────────────────────────────────────

    def load_time_slots(self, config_path: Path | str) -> list[TimeSlot]:
        """Load time slots from YAML configuration.

        Args:
            config_path: Path to time_slots.yaml.

        Returns:
            List of TimeSlot objects.
        """
        config_path = Path(config_path)
        if not config_path.exists():
            self._add_error(f"Time slots config not found: {config_path}")
            return []

        with open(config_path) as f:
            config = yaml.safe_load(f)

        slot_types = config.get("slot_types", {})
        slots_config = config.get("slots", [])
        time_slots = []

        slot_id = 1
        for slot_def in slots_config:
            slot_type = slot_def["type"]
            type_info = slot_types.get(slot_type)

            if not type_info:
                self._add_error(f"Unknown slot type: {slot_type}")
                continue

            # Get days - either from slot definition or type definition
            days = slot_def.get("days", type_info["days"])

            # For single-day slots (labs), create separate slot for each day
            if type_info.get("pattern") == "single":
                for day in days:
                    start = slot_def["start"]
                    duration = type_info["duration"]
                    end = self._calculate_end_time(start, duration)

                    time_slot = TimeSlot(
                        id=slot_id,
                        name=f"{day}_{start.replace(':', '')}",
                        pattern=day,
                        days=[day],
                        start_time=start,
                        end_time=end,
                        duration_minutes=duration,
                        slot_type=slot_type,
                    )
                    time_slots.append(time_slot)
                    slot_id += 1
            else:
                # Regular multi-day slot
                start = slot_def["start"]
                duration = type_info["duration"]
                end = self._calculate_end_time(start, duration)
                pattern = type_info["pattern"]

                time_slot = TimeSlot(
                    id=slot_id,
                    name=f"{pattern}_{start.replace(':', '')}",
                    pattern=pattern,
                    days=days,
                    start_time=start,
                    end_time=end,
                    duration_minutes=duration,
                    slot_type=slot_type,
                )
                time_slots.append(time_slot)
                slot_id += 1

        # Save to database
        for ts in time_slots:
            self.session.add(ts)
        self.session.commit()

        logger.info(f"Loaded {len(time_slots)} time slots")
        return time_slots

    def _calculate_end_time(self, start: str, duration: int) -> str:
        """Calculate end time given start time and duration.

        Args:
            start: Start time in "HH:MM" format.
            duration: Duration in minutes.

        Returns:
            End time in "HH:MM" format.
        """
        # timedelta arithmetic: the old hour-replace raised ValueError for
        # slots ending at/after midnight (e.g. 23:30 + 60min).
        start_dt = datetime.strptime(start, "%H:%M")
        end_dt = start_dt + timedelta(minutes=duration)
        return end_dt.strftime("%H:%M")

    # ─────────────────────────────────────────────────────────────────
    # Rooms
    # ─────────────────────────────────────────────────────────────────

    def load_rooms(self) -> list[Room]:
        """Load rooms from CSV."""
        df = self._read_csv("rooms.csv")
        if df is None:
            return []

        rooms = []
        for _, row in df.iterrows():
            try:
                room_type = RoomType(row["room_type"])
            except ValueError:
                self._add_error(f"Invalid room type for room {row['id']}: {row['room_type']}")
                continue

            # Optional columns: read via .get and use the value directly.
            # (Re-indexing row[col] after a .get guard crashes when the column
            # is absent, because .get returns "" and pd.notna("") is True.)
            equipment = []
            eq_raw = row.get("equipment", "")
            if pd.notna(eq_raw):
                equipment = [e.strip() for e in str(eq_raw).split(",") if e.strip()]

            is_active = row.get("is_active", True)
            if isinstance(is_active, str):
                is_active = is_active.lower() in ("true", "1", "yes")
            elif pd.isna(is_active):
                is_active = True

            room = Room(
                id=str(row["id"]),
                building=str(row["building"]),
                number=str(row["number"]),
                capacity=int(row["capacity"]),
                room_type=room_type,
                equipment=equipment,
                is_active=bool(is_active),
            )
            rooms.append(room)
            self.session.add(room)

        self.session.commit()
        logger.info(f"Loaded {len(rooms)} rooms")
        return rooms

    # ─────────────────────────────────────────────────────────────────
    # Courses
    # ─────────────────────────────────────────────────────────────────

    def load_courses(self) -> list[Course]:
        """Load courses from CSV."""
        df = self._read_csv("catalog.csv")
        if df is None:
            return []

        if "structure_type" not in df.columns:
            self._add_error(
                "catalog.csv is missing structure_type — send the schedule-type "
                "code column and we'll map it during onboarding"
            )
            return []

        courses = []
        for _, row in df.iterrows():
            try:
                structure_type = StructureType(row["structure_type"])
            except ValueError:
                self._add_error(
                    f"Invalid structure type for {row['code']}: {row['structure_type']}"
                )
                continue

            # Optional columns: read the value via .get and use it directly.
            # (Re-indexing row[col] after a .get guard crashes when the column
            # is absent, because .get returns "" and pd.notna("") is True.)
            required_room_types = []
            rrt_raw = row.get("required_room_types", "")
            if pd.notna(rrt_raw):
                required_room_types = [
                    rt.strip() for rt in str(rrt_raw).split(",") if rt.strip()
                ]

            valid_slot_types = []
            vst_raw = row.get("valid_slot_types", "")
            if pd.notna(vst_raw):
                valid_slot_types = [
                    s.strip() for s in str(vst_raw).split(",") if s.strip()
                ]

            attributes = []
            attr_raw = row.get("attributes", "")
            if pd.notna(attr_raw):
                attributes = [
                    a.strip().upper() for a in str(attr_raw).split(",") if a.strip()
                ]

            linked_code = row.get("linked_course_code")
            if pd.isna(linked_code):
                linked_code = None

            offer_terms: list[str] = []
            ot_raw = row.get("offer_terms", "")
            if pd.notna(ot_raw) and str(ot_raw).strip():
                for t in str(ot_raw).split(","):
                    t = t.strip().upper()
                    if t:
                        offer_terms.append(t)

            cadence_val = row.get("cadence", "ANNUAL")
            if pd.isna(cadence_val) or not str(cadence_val).strip():
                cadence_val = "ANNUAL"
            else:
                cadence_val = str(cadence_val).strip().upper()
            if cadence_val not in ("ANNUAL", "BIENNIAL_EVEN", "BIENNIAL_ODD"):
                self._add_error(
                    f"Invalid cadence for {row['code']}: {cadence_val} "
                    "(expected ANNUAL, BIENNIAL_EVEN, or BIENNIAL_ODD)"
                )
                cadence_val = "ANNUAL"

            # Optional per-course class-size cap (blank = use global default).
            # int(float()) tolerates "25.0" from spreadsheet exports.
            max_class_size = None
            mcs_raw = row.get("max_class_size", "")
            if pd.notna(mcs_raw) and str(mcs_raw).strip():
                try:
                    max_class_size = int(float(str(mcs_raw).strip()))
                except (ValueError, TypeError):
                    self._add_error(
                        f"Invalid max_class_size for {row['code']}: {mcs_raw}"
                    )
                    max_class_size = None
                else:
                    if max_class_size <= 0:
                        self._add_error(
                            f"Invalid max_class_size for {row['code']}: "
                            f"{mcs_raw} (must be positive)"
                        )
                        max_class_size = None

            try:
                credits = int(row["credits"])
            except (ValueError, TypeError):
                self._add_error(f"Invalid credits for {row['code']}: {row.get('credits')!r}")
                continue

            course = Course(
                code=str(row["code"]),
                title=str(row["title"]),
                credits=credits,
                structure_type=structure_type,
                linked_course_code=linked_code,
                required_room_types=required_room_types if required_room_types else ["LECTURE"],
                valid_slot_types=valid_slot_types if valid_slot_types else ["MWF_50", "TR_75"],
                min_enrollment=(
                    int(me_raw)
                    if pd.notna(me_raw := row.get("min_enrollment", 5)) and str(me_raw).strip()
                    else 5
                ),
                max_class_size=max_class_size,
                crosslist_group=(
                    str(xl_raw).strip()
                    if pd.notna(xl_raw := row.get("crosslist_group", ""))
                    and str(xl_raw).strip()
                    else None
                ),
                offer_terms=offer_terms,
                cadence=cadence_val,
                attributes=attributes,
            )
            courses.append(course)
            self.session.add(course)

        self.session.commit()
        logger.info(f"Loaded {len(courses)} courses")
        return courses

    # ─────────────────────────────────────────────────────────────────
    # Prerequisites
    # ─────────────────────────────────────────────────────────────────

    def load_prerequisites(self) -> list[Prerequisite]:
        """Load prerequisites from CSV.

        Both course codes are validated against the catalog: a typo'd code
        would otherwise become a phantom graph node that the demand
        calculator offers to every student (it has no prereqs of its own)
        and that generates demand for a course that doesn't exist.
        """
        df = self._read_csv("prerequisites.csv")
        if df is None:
            return []

        known_codes = {c.code for c in self.session.exec(select(Course)).all()}

        prereqs = []
        for _, row in df.iterrows():
            course_code = str(row["course_code"])
            prereq_code = str(row["prereq_code"])
            unknown = [c for c in (course_code, prereq_code) if c not in known_codes]
            if unknown:
                self._add_error(
                    f"Prerequisite {course_code} -> {prereq_code} references "
                    f"unknown course(s): {', '.join(unknown)}; skipped"
                )
                continue

            is_coreq = row.get("is_corequisite", False)
            if isinstance(is_coreq, str):
                is_coreq = is_coreq.lower() in ("true", "1", "yes")

            prereq = Prerequisite(
                course_code=course_code,
                prereq_code=prereq_code,
                min_grade=str(row.get("min_grade", "D")),
                is_corequisite=bool(is_coreq),
            )
            prereqs.append(prereq)
            self.session.add(prereq)

        self.session.commit()
        logger.info(f"Loaded {len(prereqs)} prerequisites")
        return prereqs

    # ─────────────────────────────────────────────────────────────────
    # Faculty
    # ─────────────────────────────────────────────────────────────────

    def load_faculty(self) -> list[Faculty]:
        """Load faculty from CSV."""
        df = self._read_csv("faculty.csv")
        if df is None:
            return []

        faculty_list = []
        for _, row in df.iterrows():
            # Optional columns — read via .get and use directly (a missing
            # column makes .get return "" which is not NA, so re-indexing
            # row[col] would KeyError).
            approved = []
            ac_raw = row.get("approved_courses", "")
            if pd.notna(ac_raw):
                approved = [c.strip() for c in str(ac_raw).split(",") if c.strip()]

            unavailable = []
            us_raw = row.get("unavailable_slots", "")
            if pd.notna(us_raw):
                unavailable = [int(s.strip()) for s in str(us_raw).split(",") if s.strip()]

            preferences = {}
            pref_raw = row.get("preferences", "")
            if pd.notna(pref_raw):
                pref_str = str(pref_raw).strip()
                if pref_str:
                    import json
                    try:
                        preferences = json.loads(pref_str)
                    except (json.JSONDecodeError, ValueError):
                        self._add_error(f"Invalid preferences JSON for {row['id']}: {pref_str}")

            mc_raw = row.get("max_credits", 12)
            max_credits = int(mc_raw) if pd.notna(mc_raw) and str(mc_raw).strip() else 12

            # Optional per-person approved overload (blank = blanket default).
            max_overload = None
            mo_raw = row.get("max_overload_credits", "")
            if pd.notna(mo_raw) and str(mo_raw).strip():
                try:
                    max_overload = int(float(str(mo_raw).strip()))
                except (ValueError, TypeError):
                    self._add_error(
                        f"Invalid max_overload_credits for {row['id']}: {mo_raw}"
                    )
                    max_overload = None
                else:
                    if max_overload < 0:
                        self._add_error(
                            f"Invalid max_overload_credits for {row['id']}: "
                            f"{mo_raw} (must be >= 0)"
                        )
                        max_overload = None

            # Faculty name/email are directory data and normally present, but
            # an over-aggressively de-identified file must not crash the load.
            fid = str(row["id"])
            fname = row.get("name", "")
            femail = row.get("email", "")
            faculty = Faculty(
                id=fid,
                name=str(fname) if pd.notna(fname) and str(fname).strip() else f"Faculty {fid}",
                email=str(femail) if pd.notna(femail) and str(femail).strip() else "",
                max_credits=max_credits,
                max_overload_credits=max_overload,
                approved_courses=approved,
                unavailable_slots=unavailable,
                preferences=preferences,
            )
            faculty_list.append(faculty)
            self.session.add(faculty)

        self.session.commit()
        logger.info(f"Loaded {len(faculty_list)} faculty members")
        return faculty_list

    # ─────────────────────────────────────────────────────────────────
    # Students
    # ─────────────────────────────────────────────────────────────────

    def load_students(self) -> list[Student]:
        """Load students from CSV."""
        df = self._read_csv("students.csv", sensitive=True)
        if df is None:
            return []

        # Defense in depth: drop direct-identifier columns before any row is
        # read, so even a raw extract accidentally delivered un-deidentified
        # cannot land a student name/email/DOB in the database.
        identifier_cols = [
            c for c in df.columns
            if str(c).strip().lower().replace(" ", "_") in STUDENT_IDENTIFIER_COLUMNS
        ]
        if identifier_cols:
            df = df.drop(columns=identifier_cols)
            logger.info(
                "students.csv: dropped identifier columns at import: %s",
                identifier_cols,
            )
            # Column NAMES are safe (and diagnostic gold: they mean the
            # extract wasn't de-identified upstream).
            self.runlog.event("identifier_columns_dropped",
                              file="students.csv", columns=identifier_cols)

        students = []
        # enumerate() gives the CSV row number (i + 2: 1-based + header) so
        # the run log can point at a row without quoting it.
        for i, (_, row) in enumerate(df.iterrows()):
            # name/email are optional: de-identified extracts (see
            # scripts/deidentify.py) drop them entirely.
            if pd.isna(row.get("id")):
                self._add_error_student(
                    "Student row with blank id skipped", "row_skipped",
                    file="students.csv", row=i + 2, reason="blank_id",
                )
                continue
            sid = str(row["id"])
            name_raw = row.get("name", "")
            email_raw = row.get("email", "")

            try:
                credits_earned = int(row["credits_earned"])
            except (ValueError, TypeError):
                self._add_error_student(
                    f"Student {sid}: invalid credits_earned "
                    f"{row.get('credits_earned')!r}; row skipped",
                    "invalid_value", file="students.csv", row=i + 2,
                    column="credits_earned",
                    shape=describe(row.get("credits_earned")),
                )
                continue

            # Registrar may omit class_standing entirely; per the data
            # request, derive it from credits_earned in that case. A
            # present-but-blank value gets the same treatment. A present,
            # unrecognized value is still an error (data quality issue, not
            # an omission).
            standing_raw = row.get("class_standing")
            if (
                "class_standing" not in row.index
                or pd.isna(standing_raw)
                or str(standing_raw).strip() == ""
            ):
                from src.core.cadence import derive_standing

                standing = derive_standing(credits_earned)
            else:
                try:
                    standing = ClassStanding(standing_raw)
                except ValueError:
                    self._add_error_student(
                        f"Invalid class standing for {sid}: {standing_raw}",
                        "invalid_value", file="students.csv", row=i + 2,
                        column="class_standing", shape=describe(standing_raw),
                    )
                    continue

            is_honors = row.get("is_honors", False)
            if isinstance(is_honors, str):
                is_honors = is_honors.lower() in ("true", "1", "yes")

            is_athlete = row.get("is_athlete", False)
            if isinstance(is_athlete, str):
                is_athlete = is_athlete.lower() in ("true", "1", "yes")

            minor = row.get("minor")
            if pd.isna(minor) or minor == "":
                minor = None

            concentration = row.get("concentration")
            if pd.isna(concentration) or concentration == "":
                concentration = None

            # Blank catalog_year in a real extract shouldn't kill the import;
            # fall back to the same default used when the column is absent.
            try:
                catalog_year = int(row.get("catalog_year", 2024))
            except (ValueError, TypeError):
                catalog_year = 2024
            major_raw = row.get("major", "")
            major = str(major_raw) if pd.notna(major_raw) else ""

            admit_term_raw = row.get("admit_term")
            admit_term = None
            if pd.notna(admit_term_raw) and str(admit_term_raw).strip():
                admit_term = str(admit_term_raw).strip()

            # Optional week-shape preference for the seat rebalancer.
            pacing = None
            pacing_raw = row.get("pacing_preference", "")
            if pd.notna(pacing_raw) and str(pacing_raw).strip():
                pacing = str(pacing_raw).strip().lower()
                if pacing not in ("cluster", "spread"):
                    self._add_error_student(
                        f"Unknown pacing_preference for {sid}: {pacing_raw!r} "
                        f"(expected cluster or spread) — using balanced default",
                        "invalid_value", file="students.csv", row=i + 2,
                        column="pacing_preference", shape=describe(pacing_raw),
                    )
                    pacing = None

            student = Student(
                id=sid,
                name=(
                    str(name_raw)
                    if pd.notna(name_raw) and str(name_raw).strip()
                    else f"Student {sid}"
                ),
                email=str(email_raw) if pd.notna(email_raw) and str(email_raw).strip() else "",
                class_standing=standing,
                credits_earned=credits_earned,
                major=major,
                concentration=concentration,
                minor=minor,
                is_honors=bool(is_honors),
                is_athlete=bool(is_athlete),
                catalog_year=catalog_year,
                pacing_preference=pacing,
                admit_term=admit_term,
            )
            students.append(student)
            self.session.add(student)

        self.session.commit()
        logger.info(f"Loaded {len(students)} students")
        return students

    # ─────────────────────────────────────────────────────────────────
    # Course History
    # ─────────────────────────────────────────────────────────────────

    def load_course_history(self) -> list[StudentCourseHistory]:
        """Load student course history from CSV."""
        df = self._read_csv("course_history.csv", sensitive=True)
        if df is None:
            return []

        history = []
        for i, (_, row) in enumerate(df.iterrows()):
            try:
                record = StudentCourseHistory(
                    student_id=str(row["student_id"]),
                    course_code=str(row["course_code"]),
                    grade=str(row["grade"]),
                    semester=str(row["semester"]),
                    credits_earned=int(row["credits_earned"]),
                )
            except (ValueError, TypeError, KeyError) as e:
                # The exception message embeds the offending cell value, so
                # the run log gets the exception TYPE plus per-column shapes —
                # enough to see "credits_earned is a float-string" without
                # ever seeing the row.
                self._add_error_student(
                    f"course_history row skipped: {e}", "row_skipped",
                    file="course_history.csv", row=i + 2,
                    exception=type(e).__name__,
                    shapes={col: describe(row.get(col)) for col in
                            ("student_id", "course_code", "grade",
                             "semester", "credits_earned")},
                )
                continue
            history.append(record)
            self.session.add(record)

        self.session.commit()
        logger.info(f"Loaded {len(history)} course history records")
        return history

    # ─────────────────────────────────────────────────────────────────
    # Degree Requirements
    # ─────────────────────────────────────────────────────────────────

    def load_degree_requirements(self) -> list[DegreeRequirement]:
        """Load degree requirements from CSV."""
        df = self._read_csv("degree_requirements.csv")
        if df is None:
            return []

        requirements = []
        for _, row in df.iterrows():
            is_required = row.get("is_required", True)
            if isinstance(is_required, str):
                is_required = is_required.lower() in ("true", "1", "yes")

            concentration = row.get("concentration")
            if pd.isna(concentration) or concentration == "":
                concentration = None

            req = DegreeRequirement(
                major=str(row["major"]),
                concentration=concentration,
                course_code=str(row["course_code"]),
                is_required=bool(is_required),
                category=str(row.get("category", "core")),
                catalog_year=int(row.get("catalog_year", 2024)),
            )
            requirements.append(req)
            self.session.add(req)

        self.session.commit()
        logger.info(f"Loaded {len(requirements)} degree requirements")
        return requirements

    # ─────────────────────────────────────────────────────────────────
    # Freshmen Projections
    # ─────────────────────────────────────────────────────────────────

    def load_freshmen_projections(self) -> list[FreshmenProjection]:
        """Load freshmen enrollment projections from CSV.

        CSV format: major,estimated_count,catalog_year
        Validates that each major has degree requirements for the given catalog year.

        Returns:
            List of FreshmenProjection objects.
        """
        df = self._read_csv("freshmen_projections.csv")
        if df is None:
            return []

        projections = []
        for _, row in df.iterrows():
            major = str(row["major"])
            catalog_year = int(row["catalog_year"])
            estimated_count = int(row["estimated_count"])

            if estimated_count <= 0:
                self._add_error(f"Invalid estimated_count for {major}: {estimated_count}")
                continue

            # Validate major has degree requirements for this catalog year
            existing_reqs = self.session.exec(
                select(DegreeRequirement).where(
                    DegreeRequirement.major == major,
                    DegreeRequirement.catalog_year == catalog_year,
                )
            ).first()

            if not existing_reqs:
                self._add_error(
                    f"No degree requirements found for major={major}, "
                    f"catalog_year={catalog_year}"
                )
                continue

            projection = FreshmenProjection(
                major=major,
                estimated_count=estimated_count,
                catalog_year=catalog_year,
                is_active=True,
            )
            projections.append(projection)
            self.session.add(projection)

        self.session.commit()
        logger.info(f"Loaded {len(projections)} freshmen projections")
        return projections

    # ─────────────────────────────────────────────────────────────────
    # Meeting Patterns
    # ─────────────────────────────────────────────────────────────────

    def load_meeting_patterns(self) -> int:
        """Load multi-meeting patterns from CSV and merge into Course records.

        CSV format: course_code,meeting_seq,valid_slot_types,required_room_types
        Example:
            BMED3301,0,"T_120","LECTURE"
            BMED3301,1,"R_360","LAB_BIO"

        This is NOT called from load_all() — it's a separate explicit step,
        like freshmen projections.

        Returns:
            Number of courses updated with meeting patterns.
        """
        df = self._read_csv("meeting_patterns.csv")
        if df is None:
            return 0

        # Group rows by course_code, ordered by meeting_seq
        patterns_by_course: dict[str, list[dict]] = {}
        for _, row in df.iterrows():
            code = str(row["course_code"])
            seq = int(row["meeting_seq"])
            slot_types = [s.strip() for s in str(row["valid_slot_types"]).split(",") if s.strip()]
            room_types = [
                r.strip() for r in str(row["required_room_types"]).split(",") if r.strip()
            ]

            if code not in patterns_by_course:
                patterns_by_course[code] = []
            patterns_by_course[code].append({
                "seq": seq,
                "slot_types": slot_types,
                "room_types": room_types,
            })

        updated = 0
        for code, patterns in patterns_by_course.items():
            course = self.session.get(Course, code)
            if not course:
                self._add_error(f"Meeting pattern references unknown course: {code}")
                continue

            # Sort by sequence and build meeting_patterns list
            patterns.sort(key=lambda p: p["seq"])
            course.meeting_patterns = [
                {"slot_types": p["slot_types"], "room_types": p["room_types"]}
                for p in patterns
            ]
            self.session.add(course)
            updated += 1

        self.session.commit()
        logger.info(f"Updated {updated} courses with meeting patterns")
        return updated

    # ─────────────────────────────────────────────────────────────────
    # Semester Guides
    # ─────────────────────────────────────────────────────────────────

    def load_semester_guides(self) -> list[SemesterGuide]:
        """Load semester guides from CSV.

        CSV format: major,concentration,catalog_year,semester_number,course_code,category,
        credits,label

        Groups rows by (major, concentration, catalog_year) to create one
        SemesterGuide per group, with SemesterGuideEntry rows for each CSV row.

        This is NOT called from load_all() — it's a separate explicit step.

        Returns:
            List of SemesterGuide objects created.
        """
        df = self._read_csv("semester_guide.csv")
        if df is None:
            return []

        # Group rows by (major, concentration, catalog_year)
        guides_data: dict[tuple[str, str | None, int], list[dict]] = {}
        for _, row in df.iterrows():
            major = str(row["major"])
            concentration = row.get("concentration")
            if pd.isna(concentration) or concentration == "":
                concentration = None
            else:
                concentration = str(concentration)
            catalog_year = int(row["catalog_year"])

            course_code = row.get("course_code")
            if pd.isna(course_code) or course_code == "":
                course_code = None
            else:
                course_code = str(course_code)

            category = row.get("category")
            if pd.isna(category) or category == "":
                category = None
            else:
                category = str(category)

            # Validation: if no course_code, must have category and label
            if not course_code and not category:
                self._add_error(
                    f"Semester guide row for {major} sem {row['semester_number']}: "
                    f"must have course_code or category"
                )
                continue

            # Validate course_code exists if specified
            if course_code:
                existing_course = self.session.get(Course, course_code)
                if not existing_course:
                    self._add_error(
                        f"Semester guide references unknown course: {course_code}"
                    )
                    continue

            label = str(row.get("label", course_code or category or ""))
            credits = int(row.get("credits", 3))

            key = (major, concentration, catalog_year)
            if key not in guides_data:
                guides_data[key] = []
            guides_data[key].append({
                "semester_number": int(row["semester_number"]),
                "course_code": course_code,
                "category": category,
                "credits": credits,
                "label": label,
            })

        # Create guides and entries
        guides = []
        for (major, concentration, catalog_year), entries in guides_data.items():
            conc_label = f"/{concentration}" if concentration else ""
            guide = SemesterGuide(
                major=major,
                concentration=concentration,
                catalog_year=catalog_year,
                name=f"{major}{conc_label} {catalog_year} Plan",
            )
            self.session.add(guide)
            # flush (not commit) assigns guide.id for the entries below while
            # keeping the whole load in ONE transaction — a parse failure on a
            # later guide must roll the wipe back, matching every other loader
            # and uploads.py's clear-then-load-atomically contract.
            self.session.flush()

            for entry_data in entries:
                entry = SemesterGuideEntry(
                    guide_id=guide.id,
                    semester_number=entry_data["semester_number"],
                    course_code=entry_data["course_code"],
                    category=entry_data["category"],
                    credits=entry_data["credits"],
                    label=entry_data["label"],
                )
                self.session.add(entry)

            guides.append(guide)

        self.session.commit()
        logger.info(f"Loaded {len(guides)} semester guides")
        return guides

    # ─────────────────────────────────────────────────────────────────
    # Banner Schedule Import
    # ─────────────────────────────────────────────────────────────────

    def import_banner_schedule(
        self,
        csv_content: str | None = None,
        csv_path: Path | str | None = None,
        schedule_name: str = "Imported Schedule",
        semester: str = "2025SP",
    ) -> ImportResult:
        """Import a Banner CSV schedule into a pinned ScheduleVersion.

        This is NOT called from load_all() — it's a separate explicit step.

        Args:
            csv_content: CSV string content (used if csv_path is None).
            csv_path: Path to CSV file (used if csv_content is None).
            schedule_name: Name for the created ScheduleVersion.
            semester: Semester code.

        Returns:
            ImportResult with counts, warnings, and errors.
        """
        result = ImportResult()

        # Read CSV
        if csv_content is not None:
            reader = csv.DictReader(StringIO(csv_content))
        elif csv_path is not None:
            csv_path = Path(csv_path)
            if not csv_path.exists():
                result.errors.append(f"File not found: {csv_path}")
                return result
            with open(csv_path, newline="") as f:
                csv_content = f.read()
            reader = csv.DictReader(StringIO(csv_content))
        else:
            result.errors.append("Must provide csv_content or csv_path")
            return result

        if reader.fieldnames:
            reader.fieldnames = [
                h.strip().upper().replace(" ", "_") for h in reader.fieldnames
            ]

        rows = list(reader)
        if not rows:
            result.errors.append("CSV is empty")
            return result

        # Build lookup indexes (query once)
        time_slot_index: dict[tuple[frozenset, str, str], TimeSlot] = {}
        for ts in self.session.exec(select(TimeSlot)).all():
            key = (frozenset(ts.days), ts.start_time, ts.end_time)
            time_slot_index[key] = ts

        room_index: dict[tuple[str, str], Room] = {}
        room_id_index: dict[str, Room] = {}
        for r in self.session.exec(select(Room)).all():
            room_index[(r.building, r.number)] = r
            room_id_index[r.id.upper()] = r

        faculty_index: dict[str, Faculty] = {}
        for f in self.session.exec(select(Faculty)).all():
            faculty_index[f.id] = f

        course_index: dict[str, Course] = {}
        for c in self.session.exec(select(Course)).all():
            course_index[c.code] = c

        # Group CSV rows by CRN
        crn_groups: dict[str, list[dict]] = {}
        for row in rows:
            crn = row["CRN"].strip()
            if crn not in crn_groups:
                crn_groups[crn] = []
            crn_groups[crn].append(row)

        # Create ScheduleVersion
        schedule = ScheduleVersion(
            name=schedule_name,
            created_at=datetime.now(),
            semester=semester,
            status="imported",
            solver_stats={},
        )
        self.session.add(schedule)
        self.session.commit()
        self.session.refresh(schedule)
        result.schedule_id = schedule.id

        # Process each CRN group
        section_counter = 0
        for crn, group_rows in crn_groups.items():
            first_row = group_rows[0]
            course_code = first_row["SUBJECT"].strip() + first_row["COURSE_NUMBER"].strip()

            # Validate course exists
            if course_code not in course_index:
                result.errors.append(f"CRN {crn}: unknown course {course_code}")
                continue

            course = course_index[course_code]

            # Backfill crosslist_group from XLST_GROUP if the catalog didn't
            # already carry one; never overwrite a differing existing value.
            xlst_group = first_row.get("XLST_GROUP", "").strip()
            if xlst_group:
                if course.crosslist_group is None:
                    course.crosslist_group = xlst_group
                    self.session.add(course)
                    result.crosslist_backfilled += 1
                elif course.crosslist_group != xlst_group:
                    result.warnings.append(
                        f"CRN {crn}: XLST_GROUP {xlst_group} conflicts with "
                        f"existing crosslist group {course.crosslist_group} "
                        f"for {course_code}"
                    )

            # Match faculty
            instructor_id = first_row.get("INSTRUCTOR_ID", "").strip()
            faculty_id = None
            if instructor_id and instructor_id in faculty_index:
                faculty_id = instructor_id
            elif instructor_id:
                result.warnings.append(
                    f"CRN {crn}: unknown faculty {instructor_id}"
                )

            # Parse each meeting row
            meeting_data = []
            for row in group_rows:
                meeting_days_str = row.get("MEETING_DAYS", "").strip()
                days = sorted(list(meeting_days_str)) if meeting_days_str else []

                start_time = row.get("START_TIME", "").strip()
                end_time = row.get("END_TIME", "").strip()

                # Match time slot
                matched_slot = None
                if days and start_time and end_time:
                    ts_key = (frozenset(days), start_time, end_time)
                    matched_slot = time_slot_index.get(ts_key)
                    if not matched_slot:
                        result.warnings.append(
                            f"CRN {crn}: no matching time slot for "
                            f"days={meeting_days_str} {start_time}-{end_time}"
                        )

                # Match room
                building = row.get("BUILDING", "").strip()
                room_num = row.get("ROOM", "").strip()
                matched_room = None
                if building and room_num:
                    matched_room = room_index.get((building, room_num))
                    if not matched_room:
                        # Banner building CODE + number is the room id
                        # convention (SCI + 104 -> SCI-104); covers rooms.csv
                        # deliveries whose building column holds the long
                        # description instead of the code.
                        matched_room = room_id_index.get(
                            f"{building}-{room_num}".upper()
                        )
                    if not matched_room:
                        result.warnings.append(
                            f"CRN {crn}: no matching room for {building} {room_num}"
                        )

                meeting_data.append({
                    "time_slot": matched_slot,
                    "room": matched_room,
                })

            # Determine capacity
            capacity_str = first_row.get("CAPACITY", "30").strip()
            try:
                capacity = int(capacity_str)
            except ValueError:
                capacity = 30

            # Create Section
            section_counter += 1
            section_number = first_row.get("SECTION", f"{section_counter:03d}").strip()

            section = Section(
                schedule_id=schedule.id,
                crn=crn,
                course_code=course_code,
                section_number=section_number,
                capacity=capacity,
                faculty_id=faculty_id,
                is_pinned=True,
                pin_reason="Imported",
            )
            self.session.add(section)
            self.session.commit()
            self.session.refresh(section)
            result.sections_imported += 1

            # Create SectionMeeting per meeting row
            for seq, md in enumerate(meeting_data):
                if md["time_slot"] and md["room"]:
                    sm = SectionMeeting(
                        section_id=section.id,
                        meeting_sequence=seq,
                        time_slot_id=md["time_slot"].id,
                        room_id=md["room"].id,
                    )
                    self.session.add(sm)
                    result.meetings_imported += 1

        self.session.commit()
        # Banner schedules are section-level (CRNs, courses, rooms, faculty) —
        # no student data, so messages are safe verbatim in the run log.
        self.runlog.event(
            "banner_import", schedule_id=result.schedule_id,
            sections=result.sections_imported,
            meetings=result.meetings_imported,
            crosslist_backfilled=result.crosslist_backfilled,
            n_errors=len(result.errors), n_warnings=len(result.warnings),
        )
        for m in result.errors:
            self.runlog.event("banner_import_error", msg=m)
        for m in result.warnings:
            self.runlog.event("banner_import_warning", msg=m)
        return result

    # ─────────────────────────────────────────────────────────────────
    # Load All
    # ─────────────────────────────────────────────────────────────────

    def load_all(self, time_slots_config: Path | str | None = None) -> dict:
        """Load all data from CSV files.

        Args:
            time_slots_config: Path to time_slots.yaml. If None, skips time slots.

        Returns:
            Dictionary with counts of loaded records.

        Raises:
            ValidationError: If any validation errors occurred.
        """
        self.errors = []  # Reset errors

        result = {
            "time_slots": 0,
            "rooms": 0,
            "courses": 0,
            "prerequisites": 0,
            "faculty": 0,
            "students": 0,
            "course_history": 0,
            "degree_requirements": 0,
        }

        # Load time slots from YAML config
        if time_slots_config:
            slots = self.load_time_slots(time_slots_config)
            result["time_slots"] = len(slots)

        # Load from CSVs
        result["rooms"] = len(self.load_rooms())
        result["courses"] = len(self.load_courses())
        result["prerequisites"] = len(self.load_prerequisites())
        result["faculty"] = len(self.load_faculty())
        result["students"] = len(self.load_students())
        result["course_history"] = len(self.load_course_history())
        result["degree_requirements"] = len(self.load_degree_requirements())

        # Check for DegreeWorks audit CSV (optional)
        for dw_name in ("audit_course_needed.csv", "degreeworks_needs.csv"):
            dw_path = self.data_dir / dw_name
            if dw_path.exists():
                result["degreeworks_csv"] = str(dw_path)
                logger.info(f"DegreeWorks audit CSV found: {dw_path}")
                break

        self.runlog.event("load_all_summary", n_errors=len(self.errors), **result)
        if self.errors:
            raise ValidationError(self.errors)

        return result


def ingest_data(
    session: Session,
    data_dir: Path | str,
    time_slots_config: Path | str | None = None,
) -> dict:
    """Convenience function to ingest all data.

    Args:
        session: Database session.
        data_dir: Directory containing CSV files.
        time_slots_config: Path to time_slots.yaml.

    Returns:
        Dictionary with counts of loaded records.
    """
    ingestor = DataIngestor(session, data_dir)
    return ingestor.load_all(time_slots_config)
