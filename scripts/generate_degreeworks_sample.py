#!/usr/bin/env python3
"""Generate a sample DegreeWorks AUDIT_COURSE_NEEDED CSV from existing sample data.

Simulates what a DegreeWorks batch audit export would look like by computing
which courses each student still needs based on their course history and degree
requirements.  Uses column names from the DegreeWorks ODS view schema.

Output: data/sample/audit_course_needed.csv
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session, select

from src.database.connection import create_tables, get_engine
from src.database.models import (
    Course,
    DegreeRequirement,
    Student,
    StudentCourseHistory,
)


def get_completed(session, student_id: str) -> set[str]:
    """Get courses a student has passed (grade D or better)."""
    passing = {"A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D"}
    history = session.exec(
        select(StudentCourseHistory).where(
            StudentCourseHistory.student_id == student_id
        )
    ).all()
    return {h.course_code for h in history if h.grade in passing}


DEFAULT_OUT_DIR = Path("data/sample")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a sample DegreeWorks AUDIT_COURSE_NEEDED CSV from "
            "existing sample data (course history + degree requirements). "
            f"By default this OVERWRITES the tracked "
            f"{DEFAULT_OUT_DIR / 'audit_course_needed.csv'}. "
            "Refuses to run without --yes."
        ))
    parser.add_argument(
        "--yes", action="store_true",
        help="Confirm overwriting the sample CSV (required to proceed)")
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="Write CSV here instead of data/sample (does not require --yes)")
    return parser.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])

    if args.out_dir is not None:
        out_dir = args.out_dir
    elif not args.yes:
        print(f"This will overwrite the tracked "
              f"{DEFAULT_OUT_DIR / 'audit_course_needed.csv'}.")
        print("Refusing to overwrite without --yes. Use --out-dir to write elsewhere.")
        return 1
    else:
        out_dir = DEFAULT_OUT_DIR

    engine = get_engine()
    create_tables(engine)

    output_path = out_dir / "audit_course_needed.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Session(engine) as session:
        students = list(session.exec(
            select(Student).where(Student.is_projected == False)  # noqa
        ).all())
        courses = {c.code: c for c in session.exec(select(Course)).all()}
        requirements = list(session.exec(select(DegreeRequirement)).all())

        # Build major -> required courses
        major_reqs: dict[str, list[tuple[str, str, bool]]] = {}
        for req in requirements:
            if req.major not in major_reqs:
                major_reqs[req.major] = []
            major_reqs[req.major].append((req.course_code, req.category, req.is_required))

        # General education courses every student needs
        gen_ed = [
            ("ENGL101", "GEN_ED", True),
            ("MATH101", "GEN_ED", True),
            ("HIST101", "GEN_ED", False),
            ("PSYC101", "GEN_ED", False),
        ]

        rows = []
        for student in students:
            completed = get_completed(session, student.id)
            all_reqs = list(major_reqs.get(student.major, [])) + gen_ed

            for course_code, category, is_required in all_reqs:
                if course_code in completed:
                    continue
                if course_code not in courses:
                    continue

                # Map to DegreeWorks block types
                block_type = {
                    "core": "MAJOR",
                    "elective": "MAJOR",
                    "lab": "MAJOR",
                    "general_ed": "OTHER",
                    "GEN_ED": "OTHER",
                }.get(category, "OTHER")

                block_value = student.major if block_type == "MAJOR" else "GEN_ED"

                # Split course code into subject + number (DW stores them separately)
                subject = course_code[:4]
                number = course_code[4:]

                rows.append({
                    "STUDENT_ID": student.id,
                    "SUBJECT": subject,
                    "COURSE_NUMBER": number,
                    "BLOCK_TYPE": block_type,
                    "BLOCK_VALUE": block_value,
                    "RULE_ID": "",  # Would be the Scribe rule sequence in real DW
                    "REQUIREMENT_TEXT": (
                        f"{'Required' if is_required else 'Elective'}: "
                        f"{courses[course_code].title}"
                    ),
                })

        # Write CSV with DegreeWorks ODS column names
        fieldnames = [
            "STUDENT_ID",
            "SUBJECT",
            "COURSE_NUMBER",
            "BLOCK_TYPE",
            "BLOCK_VALUE",
            "RULE_ID",
            "REQUIREMENT_TEXT",
        ]
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print(f"Generated {len(rows)} course-need records for {len(students)} students")
        print(f"Output: {output_path}")

        from collections import Counter
        block_counts = Counter(r["BLOCK_TYPE"] for r in rows)
        print(f"Block types: {dict(block_counts)}")
        print(f"Unique courses: {len(set(r['SUBJECT'] + r['COURSE_NUMBER'] for r in rows))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
