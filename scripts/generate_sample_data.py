#!/usr/bin/env python3
"""Generate realistic sample data for Gradimize testing.

This script creates CSV files that can be imported via the ingest module.
Designed for a small HBCU with ~1,500 students.
"""

import argparse
import csv
import random
import sys
from pathlib import Path

# Output directory (overridable via --out-dir)
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "sample"

# Random seed for reproducibility
random.seed(42)

# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────

DEPARTMENTS = {
    "BIOL": "Biology",
    "CHEM": "Chemistry",
    "PHYS": "Physics",
    "MATH": "Mathematics",
    "CSCI": "Computer Science",
    "ENGL": "English",
    "HIST": "History",
    "PSYC": "Psychology",
    "BUSN": "Business",
    "EDUC": "Education",
}

BUILDINGS = {
    "SCI": "Science Building",
    "HUM": "Humanities Building",
    "BUS": "Business Building",
    "LIB": "Library",
    "EDU": "Education Building",
}

FIRST_NAMES = [
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda",
    "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Christopher", "Nancy", "Daniel", "Lisa",
    "Marcus", "Jasmine", "Malik", "Aaliyah", "Deshawn", "Imani", "Terrell", "Kiara",
    "Andre", "Brianna", "Jalen", "Destiny", "Isaiah", "Taylor", "Xavier", "Jordan",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White",
    "Harris", "Washington", "Jefferson", "Lincoln", "Grant", "Carter", "Obama",
]


# ─────────────────────────────────────────────────────────────────
# Course Generation
# ─────────────────────────────────────────────────────────────────


def generate_courses() -> list[dict]:
    """Generate course catalog."""
    courses = []

    # BIOL courses
    courses.extend([
        {"code": "BIOL101", "title": "Introduction to Biology", "credits": 3,
         "structure_type": "PARENT", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 10},
        {"code": "BIOL101L", "title": "Introduction to Biology Lab", "credits": 1,
         "structure_type": "CHILD", "linked_course_code": "BIOL101",
         "required_room_types": "LAB_BIO", "valid_slot_types": "LAB_150",
         "min_enrollment": 10},
        {"code": "BIOL201", "title": "Cell Biology", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 8},
        {"code": "BIOL301", "title": "Genetics", "credits": 4,
         "structure_type": "COMBINED", "linked_course_code": None,
         "required_room_types": "LECTURE,LAB_BIO", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 5},
    ])

    # CHEM courses
    courses.extend([
        {"code": "CHEM101", "title": "General Chemistry I", "credits": 3,
         "structure_type": "PARENT", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 10},
        {"code": "CHEM101L", "title": "General Chemistry I Lab", "credits": 1,
         "structure_type": "CHILD", "linked_course_code": "CHEM101",
         "required_room_types": "LAB_CHEM", "valid_slot_types": "LAB_150",
         "min_enrollment": 10},
        {"code": "CHEM201", "title": "Organic Chemistry I", "credits": 3,
         "structure_type": "PARENT", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 8},
        {"code": "CHEM201L", "title": "Organic Chemistry I Lab", "credits": 1,
         "structure_type": "CHILD", "linked_course_code": "CHEM201",
         "required_room_types": "LAB_CHEM", "valid_slot_types": "LAB_150",
         "min_enrollment": 8},
    ])

    # MATH courses
    courses.extend([
        {"code": "MATH101", "title": "College Algebra", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75,MW_75",
         "min_enrollment": 15},
        {"code": "MATH102", "title": "Trigonometry", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 12},
        {"code": "MATH201", "title": "Calculus I", "credits": 4,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MTWR_50,MWF_50",
         "min_enrollment": 10},
        {"code": "MATH202", "title": "Calculus II", "credits": 4,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MTWR_50,MWF_50",
         "min_enrollment": 8},
        {"code": "MATH301", "title": "Linear Algebra", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 5},
    ])

    # CSCI courses
    courses.extend([
        {"code": "CSCI101", "title": "Introduction to Computing", "credits": 3,
         "structure_type": "PARENT", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 15},
        {"code": "CSCI101L", "title": "Introduction to Computing Lab", "credits": 1,
         "structure_type": "CHILD", "linked_course_code": "CSCI101",
         "required_room_types": "LAB_DRY", "valid_slot_types": "LAB_150",
         "min_enrollment": 15},
        {"code": "CSCI201", "title": "Data Structures", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,LAB_DRY", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 10},
        {"code": "CSCI301", "title": "Algorithms", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 8},
        {"code": "CSCI310", "title": "Database Systems", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,LAB_DRY", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 8},
    ])

    # ENGL courses
    courses.extend([
        {"code": "ENGL101", "title": "English Composition I", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,SEMINAR", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 15},
        {"code": "ENGL102", "title": "English Composition II", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,SEMINAR", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 15},
        {"code": "ENGL201", "title": "American Literature", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 10},
    ])

    # HIST courses
    courses.extend([
        {"code": "HIST101", "title": "World History I", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,AUDITORIUM", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 20},
        {"code": "HIST102", "title": "World History II", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,AUDITORIUM", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 20},
        {"code": "HIST201", "title": "African American History", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 15},
    ])

    # PSYC courses
    courses.extend([
        {"code": "PSYC101", "title": "Introduction to Psychology", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,AUDITORIUM", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 25},
        {"code": "PSYC201", "title": "Developmental Psychology", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 15},
    ])

    # BUSN courses
    courses.extend([
        {"code": "BUSN101", "title": "Introduction to Business", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 20},
        {"code": "BUSN201", "title": "Principles of Accounting", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 15},
        {"code": "BUSN301", "title": "Business Statistics", "credits": 3,
         "structure_type": "STANDALONE", "linked_course_code": None,
         "required_room_types": "LECTURE,LAB_DRY", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 12},
    ])

    # PHYS courses
    courses.extend([
        {"code": "PHYS101", "title": "Physics I", "credits": 3,
         "structure_type": "PARENT", "linked_course_code": None,
         "required_room_types": "LECTURE", "valid_slot_types": "MWF_50,TR_75",
         "min_enrollment": 10},
        {"code": "PHYS101L", "title": "Physics I Lab", "credits": 1,
         "structure_type": "CHILD", "linked_course_code": "PHYS101",
         "required_room_types": "LAB_WET", "valid_slot_types": "LAB_150",
         "min_enrollment": 10},
    ])

    return courses


def generate_prerequisites() -> list[dict]:
    """Generate prerequisite relationships."""
    prereqs = [
        # BIOL chain
        {
            "course_code": "BIOL101L",
            "prereq_code": "BIOL101",
            "min_grade": "D",
            "is_corequisite": True,
        },
        {
            "course_code": "BIOL201",
            "prereq_code": "BIOL101",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "BIOL301",
            "prereq_code": "BIOL201",
            "min_grade": "C",
            "is_corequisite": False,
        },

        # CHEM chain
        {
            "course_code": "CHEM101L",
            "prereq_code": "CHEM101",
            "min_grade": "D",
            "is_corequisite": True,
        },
        {
            "course_code": "CHEM201",
            "prereq_code": "CHEM101",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "CHEM201L",
            "prereq_code": "CHEM201",
            "min_grade": "D",
            "is_corequisite": True,
        },

        # MATH chain
        {
            "course_code": "MATH102",
            "prereq_code": "MATH101",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "MATH201",
            "prereq_code": "MATH102",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "MATH202",
            "prereq_code": "MATH201",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "MATH301",
            "prereq_code": "MATH201",
            "min_grade": "C",
            "is_corequisite": False,
        },

        # CSCI chain
        {
            "course_code": "CSCI101L",
            "prereq_code": "CSCI101",
            "min_grade": "D",
            "is_corequisite": True,
        },
        {
            "course_code": "CSCI201",
            "prereq_code": "CSCI101",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "CSCI201",
            "prereq_code": "MATH101",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "CSCI301",
            "prereq_code": "CSCI201",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "CSCI310",
            "prereq_code": "CSCI201",
            "min_grade": "C",
            "is_corequisite": False,
        },

        # ENGL chain
        {
            "course_code": "ENGL102",
            "prereq_code": "ENGL101",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "ENGL201",
            "prereq_code": "ENGL102",
            "min_grade": "C",
            "is_corequisite": False,
        },

        # HIST chain
        {
            "course_code": "HIST102",
            "prereq_code": "HIST101",
            "min_grade": "D",
            "is_corequisite": False,
        },
        {
            "course_code": "HIST201",
            "prereq_code": "HIST101",
            "min_grade": "C",
            "is_corequisite": False,
        },

        # PSYC chain
        {
            "course_code": "PSYC201",
            "prereq_code": "PSYC101",
            "min_grade": "C",
            "is_corequisite": False,
        },

        # BUSN chain
        {
            "course_code": "BUSN201",
            "prereq_code": "BUSN101",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "BUSN301",
            "prereq_code": "BUSN201",
            "min_grade": "C",
            "is_corequisite": False,
        },
        {
            "course_code": "BUSN301",
            "prereq_code": "MATH101",
            "min_grade": "C",
            "is_corequisite": False,
        },

        # PHYS chain
        {
            "course_code": "PHYS101L",
            "prereq_code": "PHYS101",
            "min_grade": "D",
            "is_corequisite": True,
        },
        {
            "course_code": "PHYS101",
            "prereq_code": "MATH101",
            "min_grade": "C",
            "is_corequisite": False,
        },
    ]
    return prereqs


def generate_rooms() -> list[dict]:
    """Generate room inventory."""
    rooms = []

    # Science Building - Labs and lecture rooms
    for i in range(1, 4):
        rooms.append({
            "id": f"SCI-10{i}", "building": "Science Building", "number": f"10{i}",
            "capacity": 35, "room_type": "LECTURE", "equipment": "projector,whiteboard",
            "is_active": True
        })

    rooms.extend([
        {"id": "SCI-201", "building": "Science Building", "number": "201",
         "capacity": 24, "room_type": "LAB_BIO", "equipment": "microscopes,specimens,sinks",
         "is_active": True},
        {"id": "SCI-202", "building": "Science Building", "number": "202",
         "capacity": 24, "room_type": "LAB_CHEM", "equipment": "fume_hoods,sinks,safety_shower",
         "is_active": True},
        {"id": "SCI-203", "building": "Science Building", "number": "203",
         "capacity": 20, "room_type": "LAB_WET", "equipment": "sinks,equipment_benches",
         "is_active": True},
        {"id": "SCI-301", "building": "Science Building", "number": "301",
         "capacity": 30, "room_type": "LAB_DRY", "equipment": "computers,projector",
         "is_active": True},
    ])

    # Humanities Building - Lecture rooms and seminar rooms
    for i in range(1, 5):
        rooms.append({
            "id": f"HUM-10{i}", "building": "Humanities Building", "number": f"10{i}",
            "capacity": 40, "room_type": "LECTURE", "equipment": "projector,whiteboard",
            "is_active": True
        })

    rooms.extend([
        {"id": "HUM-201", "building": "Humanities Building", "number": "201",
         "capacity": 20, "room_type": "SEMINAR", "equipment": "projector,whiteboard",
         "is_active": True},
        {"id": "HUM-202", "building": "Humanities Building", "number": "202",
         "capacity": 18, "room_type": "SEMINAR", "equipment": "projector,whiteboard",
         "is_active": True},
    ])

    # Business Building
    for i in range(1, 4):
        rooms.append({
            "id": f"BUS-10{i}", "building": "Business Building", "number": f"10{i}",
            "capacity": 45, "room_type": "LECTURE", "equipment": "projector,whiteboard",
            "is_active": True
        })

    rooms.append({
        "id": "BUS-201", "building": "Business Building", "number": "201",
        "capacity": 30, "room_type": "LAB_DRY", "equipment": "computers,projector",
        "is_active": True
    })

    # Library - Auditorium
    rooms.append({
        "id": "LIB-AUD", "building": "Library", "number": "AUD",
        "capacity": 150, "room_type": "AUDITORIUM", "equipment": "projector,microphone,stage",
        "is_active": True
    })

    # Education Building
    for i in range(1, 3):
        rooms.append({
            "id": f"EDU-10{i}", "building": "Education Building", "number": f"10{i}",
            "capacity": 35, "room_type": "LECTURE", "equipment": "projector,whiteboard,smartboard",
            "is_active": True
        })

    return rooms


def generate_faculty() -> list[dict]:
    """Generate faculty members."""
    faculty = []

    faculty_data = [
        # Biology
        ("FJOHNSON01", "Dr. Angela Johnson", "ajohnson@hbcu.edu", 12, "BIOL101,BIOL101L,BIOL201"),
        ("FWILLIAMS02", "Dr. Marcus Williams", "mwilliams@hbcu.edu", 12, "BIOL201,BIOL301"),

        # Chemistry
        ("FSMITH01", "Dr. Robert Smith", "rsmith@hbcu.edu", 12, "CHEM101,CHEM101L,CHEM201"),
        ("FDAVIS03", "Dr. Lisa Davis", "ldavis@hbcu.edu", 9, "CHEM201,CHEM201L"),

        # Mathematics
        ("FTHOMAS04", "Dr. James Thomas", "jthomas@hbcu.edu", 12, "MATH101,MATH102,MATH201"),
        ("FHARRIS05", "Dr. Patricia Harris", "pharris@hbcu.edu", 12, "MATH201,MATH202,MATH301"),

        # Computer Science
        (
            "FWASHINGTON06",
            "Dr. Kevin Washington",
            "kwashington@hbcu.edu",
            12,
            "CSCI101,CSCI101L,CSCI201",
        ),
        ("FJACKSON07", "Dr. Michelle Jackson", "mjackson@hbcu.edu", 12, "CSCI201,CSCI301,CSCI310"),

        # English
        ("FBROWN08", "Dr. Sarah Brown", "sbrown@hbcu.edu", 12, "ENGL101,ENGL102,ENGL201"),
        ("FMOORE09", "Prof. David Moore", "dmoore@hbcu.edu", 9, "ENGL101,ENGL102"),

        # History
        ("FTAYLOR10", "Dr. Charles Taylor", "ctaylor@hbcu.edu", 12, "HIST101,HIST102,HIST201"),

        # Psychology
        ("FANDERSON11", "Dr. Jennifer Anderson", "janderson@hbcu.edu", 12, "PSYC101,PSYC201"),

        # Business
        ("FMARTIN12", "Dr. William Martin", "wmartin@hbcu.edu", 12, "BUSN101,BUSN201"),
        ("FWHITE13", "Prof. Karen White", "kwhite@hbcu.edu", 9, "BUSN201,BUSN301"),

        # Physics
        ("FLEE14", "Dr. Michael Lee", "mlee@hbcu.edu", 12, "PHYS101,PHYS101L"),

        # Adjunct pool (can teach anything in their department)
        ("FADJUNCT01", "Prof. Adjunct Pool 1", "adjunct1@hbcu.edu", 6, ""),
        ("FADJUNCT02", "Prof. Adjunct Pool 2", "adjunct2@hbcu.edu", 6, ""),
    ]

    for fid, name, email, max_credits, approved in faculty_data:
        faculty.append({
            "id": fid,
            "name": name,
            "email": email,
            "max_credits": max_credits,
            "approved_courses": approved,
            "unavailable_slots": "",
            "preferences": ""
        })

    return faculty


def generate_students(num_students: int = 1500) -> list[dict]:
    """Generate student records."""
    students = []
    majors = ["BIOL", "CHEM", "CSCI", "MATH", "ENGL", "HIST", "PSYC", "BUSN"]

    # Distribution roughly: 30% FR, 25% SO, 25% JR, 20% SR
    standings = ["FR"] * 30 + ["SO"] * 25 + ["JR"] * 25 + ["SR"] * 20

    for i in range(1, num_students + 1):
        standing = random.choice(standings)
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)

        credits_ranges = {"FR": (0, 29), "SO": (30, 59), "JR": (60, 89), "SR": (90, 130)}
        credits_range = credits_ranges[standing]
        credits = random.randint(*credits_range)

        students.append({
            "id": f"S{i:05d}",
            "name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}{i}@student.hbcu.edu",
            "class_standing": standing,
            "credits_earned": credits,
            "major": random.choice(majors),
            "minor": random.choice(majors + [""]),
            "is_honors": random.random() < 0.1,  # 10% honors
            "is_athlete": random.random() < 0.15,  # 15% athletes
            "catalog_year": random.choice([2022, 2023, 2024, 2025])
        })

    return students


def generate_course_history(students: list[dict], courses: list[dict]) -> list[dict]:
    """Generate course history based on student standing."""
    history = []
    grades = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]
    grade_weights = [10, 8, 10, 15, 12, 10, 15, 8, 5, 5, 2]  # Weighted toward C/B

    # Typical progression by standing
    fr_courses = ["MATH101", "ENGL101"]
    so_courses = fr_courses + ["MATH102", "ENGL102", "HIST101", "PSYC101"]
    jr_courses = so_courses + ["MATH201", "HIST102", "PSYC201"]

    course_codes = [c["code"] for c in courses]
    semesters = ["2023FA", "2024SP", "2024FA", "2025SP"]

    for student in students:
        standing = student["class_standing"]
        major = student["major"]

        # Determine which courses they likely completed
        if standing == "FR":
            possible_completed = []
        elif standing == "SO":
            possible_completed = fr_courses
        elif standing == "JR":
            possible_completed = so_courses
        elif standing == "SR":
            possible_completed = jr_courses

        # Add major-specific courses
        major_100s = [c for c in course_codes if c.startswith(major) and "101" in c]
        major_200s = [c for c in course_codes if c.startswith(major) and "2" in c[4]]

        if standing in ["SO", "JR", "SR"]:
            possible_completed.extend(major_100s)
        if standing in ["JR", "SR"]:
            possible_completed.extend(major_200s[:2])

        # Actually complete a subset
        unique_possible = list(set(possible_completed))
        if unique_possible:
            max_complete = len(unique_possible)
            min_complete = min(3, max_complete)
            num_to_complete = random.randint(min_complete, max_complete)
            completed = random.sample(unique_possible, num_to_complete)
        else:
            completed = []

        for course_code in completed:
            # Don't complete labs without lecture
            if course_code.endswith("L"):
                parent = course_code[:-1]
                if parent not in completed:
                    continue

            grade = random.choices(grades, weights=grade_weights)[0]
            semester = random.choice(semesters[:-1])  # Not current semester

            course = next((c for c in courses if c["code"] == course_code), None)
            credits = course["credits"] if course else 3

            history.append({
                "student_id": student["id"],
                "course_code": course_code,
                "grade": grade,
                "semester": semester,
                "credits_earned": credits if grade != "F" else 0
            })

    return history


def generate_degree_requirements() -> list[dict]:
    """Generate degree requirements for each major."""
    requirements = []

    # BIOL major requirements
    biol_required = ["BIOL101", "BIOL101L", "BIOL201", "BIOL301", "CHEM101", "CHEM101L",
                     "MATH201", "ENGL101", "ENGL102"]
    for course in biol_required:
        requirements.append({
            "major": "BIOL", "course_code": course, "is_required": True,
            "category": "core", "catalog_year": 2024
        })

    # CSCI major requirements
    csci_required = ["CSCI101", "CSCI101L", "CSCI201", "CSCI301", "CSCI310",
                     "MATH201", "MATH202", "MATH301", "ENGL101", "ENGL102"]
    for course in csci_required:
        requirements.append({
            "major": "CSCI", "course_code": course, "is_required": True,
            "category": "core", "catalog_year": 2024
        })

    # BUSN major requirements
    busn_required = ["BUSN101", "BUSN201", "BUSN301", "MATH101", "MATH102",
                     "ENGL101", "ENGL102", "PSYC101"]
    for course in busn_required:
        requirements.append({
            "major": "BUSN", "course_code": course, "is_required": True,
            "category": "core", "catalog_year": 2024
        })

    # General education for all majors
    gen_ed = ["ENGL101", "ENGL102", "MATH101", "HIST101", "PSYC101"]
    for major in ["BIOL", "CHEM", "CSCI", "MATH", "ENGL", "HIST", "PSYC", "BUSN"]:
        for course in gen_ed:
            if not any(r["major"] == major and r["course_code"] == course for r in requirements):
                requirements.append({
                    "major": major, "course_code": course, "is_required": True,
                    "category": "general_ed", "catalog_year": 2024
                })

    return requirements


# ─────────────────────────────────────────────────────────────────
# CSV Writing
# ─────────────────────────────────────────────────────────────────


def write_csv(filename: str, data: list[dict]):
    """Write data to CSV file."""
    if not data:
        return

    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    print(f"Wrote {len(data)} records to {filepath}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate realistic sample data (catalog, prerequisites, rooms, "
            "faculty, students, course_history, degree_requirements CSVs). "
            f"By default this OVERWRITES the tracked sample CSVs under "
            f"{OUTPUT_DIR}. Refuses to run without --yes."
        ))
    parser.add_argument(
        "--yes", action="store_true",
        help="Confirm overwriting the sample CSVs (required to proceed)")
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="Write CSVs here instead of data/sample (does not require --yes)")
    return parser.parse_args(argv)


def main():
    """Generate all sample data files."""
    global OUTPUT_DIR
    args = parse_args(sys.argv[1:])

    if args.out_dir is not None:
        OUTPUT_DIR = args.out_dir
    elif not args.yes:
        print(f"This will overwrite the tracked sample CSVs under {OUTPUT_DIR}.")
        print("Refusing to overwrite without --yes. Use --out-dir to write elsewhere.")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Generate and write each dataset
    courses = generate_courses()
    write_csv("catalog.csv", courses)

    prerequisites = generate_prerequisites()
    write_csv("prerequisites.csv", prerequisites)

    rooms = generate_rooms()
    write_csv("rooms.csv", rooms)

    faculty = generate_faculty()
    write_csv("faculty.csv", faculty)

    students = generate_students(1500)
    write_csv("students.csv", students)

    course_history = generate_course_history(students, courses)
    write_csv("course_history.csv", course_history)

    requirements = generate_degree_requirements()
    write_csv("degree_requirements.csv", requirements)

    print("\nSample data generation complete!")
    print(f"Files written to: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
