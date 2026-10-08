"""DegreeWorks audit import for Gradimize.

Imports "courses still needed" data from DegreeWorks batch audit results,
replacing the internal simplified degree-requirement model with the
authoritative audit output that DegreeWorks computes (handling transfer
credits, substitutions, waivers, and complex Scribe logic).

Expected CSV format mirrors the DegreeWorks ODS view AUDIT_COURSE_NEEDED
(sourced from the CPA_CLASSNEEDED / DAP_RESULT_DTL tables).

Primary columns (DegreeWorks ODS schema):
    STUDENT_ID          — Banner student ID
    SUBJECT             — Discipline code, e.g. "BIOL"
    COURSE_NUMBER       — Course number, e.g. "101"
    BLOCK_TYPE          — DEGREE, MAJOR, MINOR, CONC, OTHER
    BLOCK_VALUE         — Block identifier, e.g. "BIOL"
    REQUIREMENT_TEXT    — Scribe rule text (optional, informational)

The importer also accepts a simplified format with a single COURSE_CODE
column instead of separate SUBJECT + COURSE_NUMBER columns.

Usage:
    from src.core.degreeworks import DegreeWorksImporter

    importer = DegreeWorksImporter(session)
    result = importer.import_audit_needs("data/audit_course_needed.csv")
"""

import csv
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from src.core.graph import PrerequisiteGraph
from src.core.urgency import score_base_urgency
from src.database.models import (
    Course,
    Student,
    StudentDemand,
)

logger = logging.getLogger(__name__)

# DegreeWorks block types that indicate required (vs elective) courses.
# DEGREE and MAJOR blocks typically contain required courses;
# OTHER blocks may be general education (also required) depending on institution.
REQUIRED_BLOCK_TYPES = {"DEGREE", "MAJOR", "MINOR", "CONC"}


class DegreeWorksImporter:
    """Imports DegreeWorks audit results and creates StudentDemand records.

    This replaces the internal DemandCalculator's degree-requirement logic
    with authoritative DegreeWorks audit output.  The urgency scoring and
    graduation-critical flagging still happen here (DegreeWorks doesn't
    produce urgency scores -- that's a scheduling concern, not an audit
    concern).
    """

    def __init__(
        self,
        session: Session,
        prereq_graph: Optional[PrerequisiteGraph] = None,
        bottleneck_threshold: int = 3,
    ):
        self.session = session
        self.graph = prereq_graph
        self.bottleneck_threshold = bottleneck_threshold

        # Cache lookups
        self._students: dict[str, Student] = {}
        self._courses: set[str] = set()
        self._bottleneck_cache: dict[str, int] = {}

    def _load_caches(self):
        """Load students and courses into memory for fast lookup."""
        if not self._students:
            for s in self.session.exec(select(Student)).all():
                self._students[s.id] = s
        if not self._courses:
            self._courses = {c.code for c in self.session.exec(select(Course)).all()}

    def _get_bottleneck_count(self, course_code: str) -> int:
        """Count how many downstream courses depend on this one."""
        if course_code in self._bottleneck_cache:
            return self._bottleneck_cache[course_code]
        count = 0
        if self.graph:
            count = len(self.graph.get_all_dependents(course_code))
        self._bottleneck_cache[course_code] = count
        return count

    def _compute_urgency(
        self,
        student: Student,
        course_code: str,
        is_required: bool,
        block_type: str,
    ) -> tuple[float, bool, str]:
        """Compute urgency score, graduation-critical flag, and reason string.

        Uses the same scoring logic as DemandCalculator but takes the
        "is this course needed?" determination from DegreeWorks rather than
        computing it internally.

        Returns:
            (urgency_score, is_graduation_critical, reason)
        """
        urgency, reasons, is_graduation_critical = score_base_urgency(
            student.class_standing,
            is_required,
            self._get_bottleneck_count(course_code),
            self.bottleneck_threshold,
        )

        # Block type context
        block_label = {
            "DEGREE": "Degree requirement",
            "MAJOR": f"Major requirement ({student.major})",
            "MINOR": "Minor requirement",
            "CONC": "Concentration requirement",
            "OTHER": "General education / other",
        }.get(block_type, block_type or "Degree requirement")
        reasons.append(block_label)

        if is_graduation_critical:
            reasons.insert(0, "GRADUATION-CRITICAL")

        return urgency, is_graduation_critical, "; ".join(reasons)

    @staticmethod
    def _detect_columns(headers: list[str]) -> dict[str, str]:
        """Map normalized CSV headers to our internal field names.

        Supports DegreeWorks ODS column names, Banner-style names, and
        simplified Gradimize names.  Returns a mapping of
        internal_name -> csv_column_name.
        """
        h_set = set(headers)
        mapping = {}

        # Student ID — try several common names
        for candidate in ("STUDENT_ID", "ID", "BANNER_ID", "PIDM", "STU_ID"):
            if candidate in h_set:
                mapping["student_id"] = candidate
                break

        # Course code — either combined or separate subject+number
        if "COURSE_CODE" in h_set:
            mapping["course_code"] = "COURSE_CODE"
        elif "SUBJECT" in h_set and "COURSE_NUMBER" in h_set:
            mapping["subject"] = "SUBJECT"
            mapping["course_number"] = "COURSE_NUMBER"
        elif "DISCIPLINE" in h_set and "COURSE_NUMBER" in h_set:
            mapping["subject"] = "DISCIPLINE"
            mapping["course_number"] = "COURSE_NUMBER"
        elif "VALUE1" in h_set and "VALUE2" in h_set:
            # Raw DAP_RESULT_DTL column names
            mapping["subject"] = "VALUE1"
            mapping["course_number"] = "VALUE2"

        # Block type
        for candidate in ("BLOCK_TYPE", "DAP_BLOCK_TYPE", "REQ_TYPE"):
            if candidate in h_set:
                mapping["block_type"] = candidate
                break

        # Block value
        for candidate in ("BLOCK_VALUE", "DAP_BLOCK_VALUE"):
            if candidate in h_set:
                mapping["block_value"] = candidate
                break

        # Required flag (optional — inferred from block type if absent)
        for candidate in ("IS_REQUIRED", "REQUIRED"):
            if candidate in h_set:
                mapping["is_required"] = candidate
                break

        return mapping

    def import_audit_needs(
        self,
        csv_path: Path | str,
        clear_existing: bool = True,
    ) -> dict:
        """Import DegreeWorks audit CSV and create StudentDemand records.

        Accepts the DegreeWorks ODS AUDIT_COURSE_NEEDED view format:
            STUDENT_ID, SUBJECT, COURSE_NUMBER, BLOCK_TYPE, BLOCK_VALUE, ...

        Also accepts simplified format:
            STUDENT_ID, COURSE_CODE, BLOCK_TYPE, IS_REQUIRED

        The importer normalizes course codes (strips spaces), skips courses
        not in the catalog, and skips students not in the student table.

        Args:
            csv_path: Path to the DegreeWorks courses-needed CSV.
            clear_existing: If True, clear existing non-projected demands first.

        Returns:
            Dict with import statistics.
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"DegreeWorks CSV not found: {csv_path}")

        self._load_caches()

        # Clear existing non-projected demands (bulk delete)
        if clear_existing:
            self.session.execute(
                sa_delete(StudentDemand).where(
                    StudentDemand.is_projected == False  # noqa: E712
                )
            )
            self.session.commit()

        # Read CSV
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                reader.fieldnames = [
                    h.strip().upper().replace(" ", "_") for h in reader.fieldnames
                ]
            rows = list(reader)

        if not rows:
            return {"students": 0, "demands_created": 0, "rows_read": 0,
                    "skipped_no_student": 0, "skipped_no_course": 0, "skipped_duplicate": 0}

        logger.info(f"Read {len(rows)} rows from {csv_path}")

        # Detect column mapping
        col_map = self._detect_columns(list(rows[0].keys()))
        if "student_id" not in col_map:
            raise ValueError(
                f"Cannot find student ID column in {csv_path}. "
                f"Headers: {list(rows[0].keys())}"
            )
        if "course_code" not in col_map and "subject" not in col_map:
            raise ValueError(
                f"Cannot find course code columns in {csv_path}. "
                f"Expected COURSE_CODE or SUBJECT+COURSE_NUMBER. "
                f"Headers: {list(rows[0].keys())}"
            )

        logger.info(f"Column mapping: {col_map}")

        demands_created = 0
        students_seen: set[str] = set()
        skipped_no_student = 0
        skipped_no_course = 0
        seen_pairs: set[tuple[str, str]] = set()
        if not clear_existing:
            # Existing demands stay in place, so dedup against them too —
            # not just rows within this CSV — or a re-import would create
            # duplicate (student, course) demand rows.
            seen_pairs.update(
                (d.student_id, d.course_code)
                for d in self.session.exec(select(StudentDemand)).all()
            )

        for i, row in enumerate(rows):
            # Student ID
            student_id = row.get(col_map["student_id"], "").strip()
            if not student_id:
                continue

            # Course code
            if "course_code" in col_map:
                course_code = row.get(col_map["course_code"], "").strip().replace(" ", "")
            else:
                subject = row.get(col_map["subject"], "").strip()
                number = row.get(col_map["course_number"], "").strip()
                course_code = f"{subject}{number}"

            if not course_code:
                continue

            # Validate student
            student = self._students.get(student_id)
            if not student:
                skipped_no_student += 1
                continue

            # Validate course
            if course_code not in self._courses:
                skipped_no_course += 1
                continue

            # Dedup
            pair = (student_id, course_code)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            # Block type
            block_type = "MAJOR"
            if "block_type" in col_map:
                block_type = row.get(col_map["block_type"], "MAJOR").strip().upper()

            # Required flag — use explicit column if present, otherwise infer from block type
            if "is_required" in col_map:
                req_str = row.get(col_map["is_required"], "Y").strip().upper()
                is_required = req_str in ("Y", "YES", "TRUE", "1", "T")
            else:
                is_required = block_type in REQUIRED_BLOCK_TYPES

            # Compute urgency
            urgency, is_grad_critical, reason = self._compute_urgency(
                student, course_code, is_required, block_type
            )

            demand = StudentDemand(
                student_id=student_id,
                course_code=course_code,
                urgency_score=urgency,
                is_required=is_required,
                is_graduation_critical=is_grad_critical,
                is_projected=False,
                reason=f"[DegreeWorks] {reason}",
            )
            self.session.add(demand)
            demands_created += 1
            students_seen.add(student_id)

            if (i + 1) % 500 == 0:
                self.session.commit()

        self.session.commit()

        result = {
            "students": len(students_seen),
            "demands_created": demands_created,
            "rows_read": len(rows),
            "skipped_no_student": skipped_no_student,
            "skipped_no_course": skipped_no_course,
            "skipped_duplicate": (
                len(rows) - demands_created - skipped_no_student - skipped_no_course
            ),
        }
        logger.info(
            f"DegreeWorks import: {demands_created} demands for "
            f"{len(students_seen)} students "
            f"(skipped {skipped_no_student} unknown students, "
            f"{skipped_no_course} unknown courses)"
        )
        return result
