# Gradimize: Academic Scheduling Engine

## Project Context

**Institution:** Small HBCU (~1,500 students)
**Problem:** Manual course scheduling causes bottlenecks, conflicts, and delayed graduations
**Solution:** Constraint-programming-based scheduler that maximizes student progression

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Gradimize                                │
├──────────────────────┬──────────────────────────────────────────┤
│  TUI (Textual)       │  React web UI (web/)                      │
│  - Dashboard         │        │                                  │
│  - Data browser      │        ▼                                  │
│  - Constraint editor │  REST API (FastAPI, src/api)              │
│  - Schedule viewer   │  - /schedule/*  /data/*  /finals/*        │
│         │            │  - SSE streaming of solver progress       │
│         │            │        │                                  │
├─────────┴────────────┴────────┴──────────────────────────────────┤
│                   Core Engine (src/core)                         │
│  ┌──────────┐  ┌────────┐  ┌──────────┐  ┌──────────┐            │
│  │  Ingest  │→ │ Demand │→ │  Solver  │→ │  Analyze │            │
│  │ (Pandas) │  │+ Graph │  │(OR-Tools)│  │ / Export │            │
│  └──────────┘  └────────┘  └──────────┘  └──────────┘            │
├─────────────────────────────────────────────────────────────────┤
│                   SQLite + SQLModel (src/database)               │
└─────────────────────────────────────────────────────────────────┘
```

The TUI calls the core engine directly; the React web UI reaches it through the
FastAPI layer. Both front ends are thin — all scheduling logic lives in
`src/core`, whose `__init__.py` has a module-by-module map.

**Tech Stack:**
- Python 3.11+
- SQLModel (Pydantic + SQLAlchemy)
- OR-Tools CP-SAT solver
- NetworkX for prerequisite graphs
- Pandas for data manipulation
- FastAPI for REST endpoints
- React + TypeScript + Vite for the web UI
- Textual for TUI
- SQLite for persistence

---

## 2. Domain Model

### 2.1 Time Representation

We discretize time into **TimeSlots** rather than continuous minutes. This converts the problem to Integer Programming, which solves faster.

**Standard patterns at most institutions:**

| Pattern | Days | Duration | Typical Use |
|---------|------|----------|-------------|
| MWF_50  | Mon, Wed, Fri | 50 min | 3-credit lecture |
| TR_75   | Tue, Thu | 75 min | 3-credit lecture |
| MW_75   | Mon, Wed | 75 min | 3-credit lecture |
| T_170   | Tue only | 170 min | Lab |
| R_170   | Thu only | 170 min | Lab |
| W_170   | Wed only | 170 min | Lab |
| MTWR_50 | Mon-Thu | 50 min | 4-credit lecture |

**Slot overlap detection:** Two slots conflict if they share any day AND their time ranges intersect. This must be precomputed into a conflict matrix.

### 2.2 Course Structure Types

Courses have a `structure_type` that determines scheduling constraints:

```
STANDALONE  - Normal course. No linking requirements.
              Example: HIST 101 (3 credits, any standard slot)

COMBINED    - Lecture + Lab in single CRN. Needs extended slot.
              Example: CHEM 111 (4 credits, needs 200+ min block)

PARENT      - Lecture component that has linked lab sections.
              Example: BIO 101 (3 credits lecture)
              Constraint: Cannot overlap with any of its CHILD sections

CHILD       - Lab component linked to a PARENT.
              Example: BIO 101L (1 credit lab)
              Constraint: Cannot overlap with its PARENT
              Constraint: Sum(child capacities) >= parent capacity
```

### 2.3 Room Types

Rooms have capabilities that must match course requirements:

```
LECTURE      - Standard classroom with board/projector
LAB_WET      - Science lab with sinks, fume hoods, etc.
LAB_DRY      - Computer lab or engineering lab
LAB_BIO      - Biology-specific (microscopes, specimens)
LAB_CHEM     - Chemistry-specific (fume hoods, safety equipment)
SEMINAR      - Small discussion room
AUDITORIUM   - Large lecture hall
```

**Constraint:** A section can only be assigned to a room where `room.type IN section.allowed_room_types`.

---

## 3. Data Schema

### 3.1 SQLModel Definitions

```python
# src/database/models.py

from datetime import time
from typing import Optional, List
from sqlmodel import SQLModel, Field, Column, JSON
from enum import Enum

class StructureType(str, Enum):
    STANDALONE = "STANDALONE"
    COMBINED = "COMBINED"
    PARENT = "PARENT"
    CHILD = "CHILD"

class RoomType(str, Enum):
    LECTURE = "LECTURE"
    LAB_WET = "LAB_WET"
    LAB_DRY = "LAB_DRY"
    LAB_BIO = "LAB_BIO"
    LAB_CHEM = "LAB_CHEM"
    SEMINAR = "SEMINAR"
    AUDITORIUM = "AUDITORIUM"

class ClassStanding(str, Enum):
    FR = "FR"
    SO = "SO"
    JR = "JR"
    SR = "SR"

# ─────────────────────────────────────────────────────────────────
# Core Entities
# ─────────────────────────────────────────────────────────────────

class TimeSlot(SQLModel, table=True):
    """A discrete scheduling block (e.g., MWF 8:00-8:50)"""
    id: int = Field(primary_key=True)
    name: str                          # "MWF_0800" for display
    pattern: str                       # "MWF", "TR", "M", etc.
    days: List[str] = Field(sa_column=Column(JSON))  # ["M", "W", "F"]
    start_time: str                    # "08:00" (ISO format)
    end_time: str                      # "08:50"
    duration_minutes: int              # 50
    slot_type: str                     # "MWF_50", "TR_75", "LAB_170"

class Room(SQLModel, table=True):
    """Physical space where classes meet"""
    id: str = Field(primary_key=True)  # "SCI-201"
    building: str                      # "Science Building"
    number: str                        # "201"
    capacity: int
    room_type: RoomType
    equipment: List[str] = Field(default=[], sa_column=Column(JSON))
    is_active: bool = True

class Course(SQLModel, table=True):
    """Catalog entry (e.g., BIO 101)"""
    code: str = Field(primary_key=True)       # "BIO101"
    title: str                                 # "Introduction to Biology"
    credits: int
    structure_type: StructureType
    linked_course_code: Optional[str] = None  # If CHILD, points to PARENT
    required_room_types: List[RoomType] = Field(
        default=[RoomType.LECTURE],
        sa_column=Column(JSON)
    )
    valid_slot_types: List[str] = Field(
        default=["MWF_50", "TR_75"],
        sa_column=Column(JSON)
    )
    min_enrollment: int = 5                   # Cancel if below this
    
class Faculty(SQLModel, table=True):
    """Instructor"""
    id: str = Field(primary_key=True)         # "FSMITH01"
    name: str
    email: str
    max_credits: int = 12                     # Per semester
    approved_courses: List[str] = Field(
        default=[],
        sa_column=Column(JSON)
    )  # Course codes they can teach
    unavailable_slots: List[int] = Field(
        default=[],
        sa_column=Column(JSON)
    )  # TimeSlot IDs they cannot teach
    preferences: dict = Field(
        default={},
        sa_column=Column(JSON)
    )  # Soft preferences (weighted in objective)

class Student(SQLModel, table=True):
    """Enrolled student"""
    id: str = Field(primary_key=True)         # Banner ID
    name: str
    email: str
    class_standing: ClassStanding
    credits_earned: int
    major: str
    minor: Optional[str] = None
    is_honors: bool = False
    is_athlete: bool = False
    catalog_year: int                         # Determines which requirements apply

# ─────────────────────────────────────────────────────────────────
# Relationships & Derived Data
# ─────────────────────────────────────────────────────────────────

class Prerequisite(SQLModel, table=True):
    """Course dependency edge"""
    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(foreign_key="course.code")
    prereq_code: str = Field(foreign_key="course.code")
    min_grade: str = "D"                      # Minimum grade required
    is_corequisite: bool = False              # Can be taken concurrently

class StudentCourseHistory(SQLModel, table=True):
    """Courses a student has completed"""
    id: Optional[int] = Field(default=None, primary_key=True)
    student_id: str = Field(foreign_key="student.id")
    course_code: str = Field(foreign_key="course.code")
    grade: str
    semester: str                             # "2024FA"
    credits_earned: int

class StudentDemand(SQLModel, table=True):
    """Derived: Which courses each student needs, with priority scores"""
    id: Optional[int] = Field(default=None, primary_key=True)
    student_id: str = Field(foreign_key="student.id")
    course_code: str = Field(foreign_key="course.code")
    urgency_score: float                      # Higher = more urgent
    is_required: bool                         # vs elective
    reason: str                               # "Major requirement", "Prereq for X"

# ─────────────────────────────────────────────────────────────────
# Schedule Output
# ─────────────────────────────────────────────────────────────────

class ScheduleVersion(SQLModel, table=True):
    """A complete schedule (for versioning/comparison)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str                                 # "Draft A", "Final"
    created_at: str                           # ISO timestamp
    semester: str                             # "2025SP"
    status: str                               # "draft", "approved", "archived"
    solver_stats: dict = Field(default={}, sa_column=Column(JSON))

class Section(SQLModel, table=True):
    """A scheduled offering of a course (solver output)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(foreign_key="scheduleversion.id")
    crn: str                                  # Unique for Banner export
    course_code: str = Field(foreign_key="course.code")
    section_number: str                       # "001", "002"
    capacity: int
    is_honors: bool = False
    # Assigned by solver:
    time_slot_id: Optional[int] = Field(default=None, foreign_key="timeslot.id")
    room_id: Optional[str] = Field(default=None, foreign_key="room.id")
    faculty_id: Optional[str] = Field(default=None, foreign_key="faculty.id")
    # Manual overrides:
    is_pinned: bool = False                   # Don't let solver move this
    pin_reason: Optional[str] = None

class SectionEnrollment(SQLModel, table=True):
    """Student assigned to section (solver output)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    section_id: int = Field(foreign_key="section.id")
    student_id: str = Field(foreign_key="student.id")
    is_confirmed: bool = False                # vs tentative assignment
```

### 3.2 Configuration Files

**`config/time_slots.yaml`**
```yaml
# Standard time slot definitions
# Duration in minutes, times in 24h format

slot_types:
  MWF_50:
    pattern: "MWF"
    days: ["M", "W", "F"]
    duration: 50
    
  TR_75:
    pattern: "TR"
    days: ["T", "R"]
    duration: 75
    
  MW_75:
    pattern: "MW"
    days: ["M", "W"]
    duration: 75
    
  LAB_170:
    pattern: "single"
    days: ["M", "T", "W", "R", "F"]  # Any single day
    duration: 170

# Actual slots generated from types
slots:
  # MWF blocks
  - type: MWF_50
    start: "08:00"
  - type: MWF_50
    start: "09:00"
  - type: MWF_50
    start: "10:00"
  - type: MWF_50
    start: "11:00"
  - type: MWF_50
    start: "12:00"
  - type: MWF_50
    start: "13:00"
  - type: MWF_50
    start: "14:00"
  - type: MWF_50
    start: "15:00"
    
  # TR blocks
  - type: TR_75
    start: "08:00"
  - type: TR_75
    start: "09:30"
  - type: TR_75
    start: "11:00"
  - type: TR_75
    start: "12:30"
  - type: TR_75
    start: "14:00"
  - type: TR_75
    start: "15:30"
    
  # Lab blocks (Tuesday)
  - type: LAB_170
    days: ["T"]
    start: "08:00"
  - type: LAB_170
    days: ["T"]
    start: "14:00"
    
  # Lab blocks (Wednesday)
  - type: LAB_170
    days: ["W"]
    start: "08:00"
  - type: LAB_170
    days: ["W"]
    start: "14:00"
    
  # Lab blocks (Thursday)
  - type: LAB_170
    days: ["R"]
    start: "08:00"
  - type: LAB_170
    days: ["R"]
    start: "14:00"
```

**`config/constraints.yaml`**
```yaml
# Hard constraints (must be satisfied)
hard_constraints:
  # Room can't double-book
  room_no_overlap: true
  
  # Faculty can't double-book
  faculty_no_overlap: true
  
  # Enrollment can't exceed the assigned room's capacity
  respect_room_capacity: true
  
  # Course must go in compatible room type
  match_room_type: true
  
  # Faculty can only teach approved courses
  faculty_approval_required: true
  
  # Linked courses (PARENT/CHILD) can't overlap
  linked_no_overlap: true
  
  # Minimum travel time between buildings (minutes)
  min_travel_time: 10

# NOTE: student schedule overlap is not a constraint of the JOINT model
# (per-student clauses explode at scale); high-conflict course pairs are
# kept apart via soft penalties gated by conflict_urgency_threshold, and the
# exact sectioning pass (solver.exact_sectioning, src/core/sectioning.py)
# then enforces it exactly on the fixed timetable. The pass is LP-guided and
# reports an honest gap to the LP bound.

# Soft constraints (preferences, weighted in objective)
soft_constraints:
  # Prefer not to schedule before this hour
  early_morning_penalty:
    hour: 8
    weight: -10
    
  # Prefer not to schedule after this hour
  late_afternoon_penalty:
    hour: 17
    weight: -10
    
  # Penalty per adjacent back-to-back teaching pair
  # (faculty with preferences.allow_back_to_back are exempt)
  back_to_back_penalty:
    faculty:
      weight: -20
    
  # Bonus for honoring faculty preferences
  faculty_preference_bonus:
    weight: 5
    
  # Penalty for uneven course distribution across days
  day_balance_penalty:
    weight: -15

# Conflict threshold: course pairs whose summed student-overlap urgency
# exceeds this get soft penalties against overlapping time slots
conflict_urgency_threshold: 50.0

# Section sizing (create_sections + capacity constraints)
section_settings:
  default_capacity: 30       # seats per section in "fixed" mode
  students_per_section: 25   # one extra section per this many demands
  max_sections_per_course: 5
  crn_start: 10001
  # "fixed": sections seat default_capacity (or the course's max_class_size).
  # "room": class size floats up to the assigned room's capacity, clamped by
  # the course's max_class_size (or the global one below); the stored section
  # capacity then reflects the room actually chosen.
  capacity_mode: fixed
  max_class_size:            # global ceiling in room mode (blank = none)
```

---

## 4. Algorithm Pipeline

### 4.1 Phase 1: Ingestion

```
Banner CSV → Parse → Validate → Load to SQLite
DegreeWorks CSV → Parse → Validate → Load to SQLite
```

**Expected input files:**
- `students.csv`: Student demographics
- `course_history.csv`: Completed courses with grades
- `catalog.csv`: Course definitions
- `prerequisites.csv`: Prereq relationships
- `faculty.csv`: Faculty info and approved courses
- `rooms.csv`: Room inventory
- `degree_requirements.csv`: What courses each major needs

**Validation rules:**
- All foreign keys must resolve
- No duplicate primary keys
- Enum values must be valid
- Credit hours must be positive integers

### 4.2 Phase 2: Demand Calculation

```python
def calculate_student_demand(student: Student, graph: nx.DiGraph) -> List[StudentDemand]:
    """
    Determine which courses a student needs and how urgently.
    
    Urgency formula:
        base_score = {SR: 100, JR: 50, SO: 25, FR: 10}[standing]
        
        for each needed_course:
            urgency = base_score
            
            # Bottleneck multiplier: courses that unlock many others
            downstream_count = len(nx.descendants(graph, course))
            if downstream_count > 3:
                urgency *= 2.0
            
            # Graduation proximity
            if course in final_semester_courses:
                urgency *= 3.0
                
            # Required vs elective
            if course in major_requirements:
                urgency *= 1.5
    """
```

### 4.3 Phase 3: Conflict Matrix

```python
def build_conflict_matrix(demands: List[StudentDemand]) -> pd.DataFrame:
    """
    Build Course x Course matrix where cell value = 
    sum of urgency scores for students who need BOTH courses.
    
    If cell value > THRESHOLD, these courses get a hard
    constraint to never overlap.
    """
    courses = list(set(d.course_code for d in demands))
    matrix = pd.DataFrame(0.0, index=courses, columns=courses)
    
    # Group demands by student
    by_student = defaultdict(list)
    for d in demands:
        by_student[d.student_id].append(d)
    
    # For each student, add their urgency to each course pair
    for student_id, student_demands in by_student.items():
        for i, d1 in enumerate(student_demands):
            for d2 in student_demands[i+1:]:
                # Use minimum urgency of the pair
                conflict_weight = min(d1.urgency_score, d2.urgency_score)
                matrix.loc[d1.course_code, d2.course_code] += conflict_weight
                matrix.loc[d2.course_code, d1.course_code] += conflict_weight
    
    return matrix
```

### 4.4 Phase 4: CP-SAT Solver

**Decision Variables:**

```python
# For each section s, time slot t, room r:
# X[s,t,r] = 1 if section s is scheduled in slot t in room r
X = {}
for section in sections:
    for slot in compatible_slots[section]:
        for room in compatible_rooms[section]:
            X[section.id, slot.id, room.id] = model.NewBoolVar(
                f'X_{section.id}_{slot.id}_{room.id}'
            )

# For each section s, faculty f:
# Y[s,f] = 1 if section s is taught by faculty f
Y = {}
for section in sections:
    for faculty in approved_faculty[section.course_code]:
        Y[section.id, faculty.id] = model.NewBoolVar(
            f'Y_{section.id}_{faculty.id}'
        )

# For each student enrollment possibility:
# Z[student, section] = 1 if student is assigned to section
Z = {}
for demand in student_demands:
    for section in sections_for_course[demand.course_code]:
        Z[demand.student_id, section.id] = model.NewBoolVar(
            f'Z_{demand.student_id}_{section.id}'
        )
```

**Hard Constraints:**

```python
# 1. Each section must be assigned exactly one (slot, room) pair
for section in sections:
    if not section.is_pinned:
        model.Add(
            sum(X[section.id, t, r] 
                for t in compatible_slots[section]
                for r in compatible_rooms[section]) == 1
        )

# 2. Each section must have exactly one faculty
for section in sections:
    model.Add(
        sum(Y[section.id, f] 
            for f in approved_faculty[section.course_code]) == 1
    )

# 3. Room can't double-book (same slot, same room, different sections)
for slot in all_slots:
    for room in all_rooms:
        model.Add(
            sum(X[s.id, slot.id, room.id] 
                for s in sections 
                if (s.id, slot.id, room.id) in X) <= 1
        )

# 4. Faculty can't double-book
for slot in all_slots:
    for faculty in all_faculty:
        # Get all sections this faculty might teach
        possible_sections = [s for s in sections 
                           if faculty.id in approved_faculty[s.course_code]]
        
        for s1, s2 in combinations(possible_sections, 2):
            # If both are assigned to this faculty and same slot → conflict
            for room1 in compatible_rooms[s1]:
                for room2 in compatible_rooms[s2]:
                    if (s1.id, slot.id, room1.id) in X and \
                       (s2.id, slot.id, room2.id) in X:
                        model.Add(
                            X[s1.id, slot.id, room1.id] + 
                            X[s2.id, slot.id, room2.id] + 
                            Y[s1.id, faculty.id] + 
                            Y[s2.id, faculty.id] <= 3
                        )

# 5. Linked courses (PARENT/CHILD) can't overlap
for parent in sections:
    if parent.course.structure_type == StructureType.PARENT:
        children = [s for s in sections 
                   if s.course.linked_course_code == parent.course_code]
        for child in children:
            for slot in all_slots:
                # If slots overlap (precomputed)
                overlapping_slots = get_overlapping_slots(slot)
                for overlap_slot in overlapping_slots:
                    for r1 in compatible_rooms[parent]:
                        for r2 in compatible_rooms[child]:
                            if (parent.id, slot.id, r1.id) in X and \
                               (child.id, overlap_slot.id, r2.id) in X:
                                model.Add(
                                    X[parent.id, slot.id, r1.id] + 
                                    X[child.id, overlap_slot.id, r2.id] <= 1
                                )

# 6. High-conflict course pairs can't overlap
for c1, c2 in get_high_conflict_pairs(conflict_matrix, threshold):
    sections_c1 = sections_for_course[c1]
    sections_c2 = sections_for_course[c2]
    for s1 in sections_c1:
        for s2 in sections_c2:
            for slot in all_slots:
                overlapping = get_overlapping_slots(slot)
                for overlap_slot in overlapping:
                    # ... similar constraint as above

# 7. Faculty load limits
for faculty in all_faculty:
    model.Add(
        sum(Y[s.id, faculty.id] * s.course.credits 
            for s in sections 
            if (s.id, faculty.id) in Y) <= faculty.max_credits
    )

# 8. Capacity constraints
for section in sections:
    for slot in compatible_slots[section]:
        for room in compatible_rooms[section]:
            # Effective capacity is min of section cap and room cap
            eff_cap = min(section.capacity, room.capacity)
            model.Add(
                sum(Z[student_id, section.id] 
                    for student_id in students_needing[section.course_code]
                    if (student_id, section.id) in Z) <= eff_cap
            ).OnlyEnforceIf(X[section.id, slot.id, room.id])

# 9. Student can't be in overlapping sections
for student in all_students:
    student_sections = [s for s in sections 
                       if (student.id, s.id) in Z]
    for s1, s2 in combinations(student_sections, 2):
        for slot1 in compatible_slots[s1]:
            for slot2 in get_overlapping_slots(slot1):
                if slot2 in compatible_slots[s2]:
                    for r1 in compatible_rooms[s1]:
                        for r2 in compatible_rooms[s2]:
                            model.Add(
                                Z[student.id, s1.id] + 
                                Z[student.id, s2.id] + 
                                X[s1.id, slot1.id, r1.id] + 
                                X[s2.id, slot2.id, r2.id] <= 3
                            )

# 10. Pinned sections (manual overrides)
for section in sections:
    if section.is_pinned:
        model.Add(X[section.id, section.time_slot_id, section.room_id] == 1)
        if section.faculty_id:
            model.Add(Y[section.id, section.faculty_id] == 1)
```

**Objective Function:**

```python
objective_terms = []

# PRIMARY: Maximize weighted student enrollment
for demand in student_demands:
    for section in sections_for_course[demand.course_code]:
        if (demand.student_id, section.id) in Z:
            objective_terms.append(
                demand.urgency_score * Z[demand.student_id, section.id]
            )

# SECONDARY: Soft constraint penalties/bonuses
# (Faculty preferences, time preferences, etc.)
# These should be much smaller than urgency scores

model.Maximize(sum(objective_terms))
```

---

## 5. Infeasibility Handling

The solver may fail to find a solution. Here's the recovery strategy:

### 5.1 Detection

```python
status = solver.Solve(model)

if status == cp_model.INFEASIBLE:
    # Solver proved no solution exists
    return handle_infeasibility()
    
elif status == cp_model.MODEL_INVALID:
    # Model has errors (bug in our code)
    raise ModelError(solver.ResponseStats())
    
elif status == cp_model.UNKNOWN:
    # Timeout or resource limit
    return handle_timeout()
```

### 5.2 Diagnosis

When infeasible, identify the likely cause:

```python
def diagnose_infeasibility():
    """
    Run progressively relaxed models to find the constraint(s)
    that make the problem infeasible.
    """
    
    # Test 1: Ignore student demand entirely
    # Just try to place all sections with room/faculty constraints
    if not can_place_all_sections():
        return "RESOURCE_SHORTAGE"  # Not enough rooms or faculty
    
    # Test 2: Allow room type mismatches
    if not can_place_with_flexible_rooms():
        return "ROOM_TYPE_MISMATCH"  # Labs need lab rooms
    
    # Test 3: Allow faculty overloads
    if not can_place_with_flexible_faculty():
        return "FACULTY_OVERLOAD"  # Not enough qualified faculty
    
    # Test 4: Relax conflict constraints
    if not can_place_with_fewer_conflicts():
        return "CONFLICT_OVERLOAD"  # Too many courses must not overlap
    
    return "UNKNOWN"
```

### 5.3 Recovery Options

Based on diagnosis, offer actionable solutions:

| Diagnosis | Suggested Actions |
|-----------|-------------------|
| RESOURCE_SHORTAGE | Add rooms, add time slots, reduce section count |
| ROOM_TYPE_MISMATCH | Add lab rooms, convert rooms, schedule labs off-peak |
| FACULTY_OVERLOAD | Hire adjuncts, increase load limits, reduce sections |
| CONFLICT_OVERLOAD | Raise conflict threshold, add sections of bottleneck courses |

---

## 6. Project Structure

High level (kept deliberately short — the authoritative, per-module maps live in
each package's `__init__.py`; `src/core/__init__.py` is the one to read first, and
`README.md` has the top-level layout):

```
course_opt/
├── config/       time_slots.yaml, constraints.yaml   (solver configuration)
├── data/         sample input CSVs + generated output DBs
├── src/
│   ├── core/     the domain engine — ingest → demand → solve → analyze → export
│   │             (~30 modules; see src/core/__init__.py for the module map)
│   ├── database/ models.py (SQLModel tables) + connection.py + queries.py
│   ├── api/      FastAPI backend: main.py, schemas.py, sse.py, routes/
│   └── tui/      Textual UI: app.py + screens/  (calls core directly)
├── web/          React + TypeScript frontend (served by the API)
├── tests/        pytest suite
└── scripts/      sample-data generation, benchmarking, one-off analysis
```

Note: the core engine is no longer just ingest → graph → solver → export. Demand
modeling now has three sources (predicted / DegreeWorks / registrations), and a
family of post-solve analyzers (validate, schedule_quality, viability, recommend,
advisor) and what-if runners (scenarios, stress, profiles, longrange, replan) sit
alongside the solver. The `src/core/__init__.py` docstring groups them by layer.

---

## 7. Implementation Sequence

Build in this order to enable incremental testing:

### Phase 1: Foundation (Week 1)
1. **Project setup**: Poetry, dependencies, directory structure
2. **Models**: Implement all SQLModel classes
3. **Database**: SQLite connection, table creation
4. **Sample data generator**: Create realistic test dataset

### Phase 2: Data Pipeline (Week 2)
5. **Ingest**: CSV parsers with validation
6. **Graph**: NetworkX prerequisite DAG
7. **Demand**: Urgency score calculation
8. **Conflicts**: Build conflict matrix

### Phase 3: Solver (Week 3)
9. **Engine core**: Basic CP-SAT with room/faculty constraints
10. **Student assignment**: Add enrollment variables
11. **Linked courses**: PARENT/CHILD constraints
12. **Soft constraints**: Preferences and penalties

### Phase 4: Interface (Week 4)
13. **API**: FastAPI endpoints
14. **TUI dashboard**: Basic stats view
15. **Schedule viewer**: Grid display
16. **Export**: Banner CSV format

### Phase 5: Polish (Week 5)
17. **Infeasibility handling**: Diagnosis and recovery
18. **Schedule comparison**: Version diffing
19. **Performance optimization**: Large dataset testing
20. **Documentation**: User guide

---

## 8. Testing Strategy

### Unit Tests
- Model validation (reject bad data)
- Urgency score calculation (known inputs → expected outputs)
- Conflict matrix symmetry
- Time slot overlap detection

### Integration Tests
- Full pipeline on small dataset (10 students, 5 courses)
- Solver finds valid solution
- All hard constraints satisfied
- Export parses correctly

### Stress Tests
- 1,500 students, 200 courses, 50 faculty
- Solver completes in < 5 minutes
- Memory usage stays reasonable

### Constraint Tests
Each hard constraint needs a test proving it works:
```python
def test_room_no_double_book():
    """Create scenario where two sections want same room/slot.
    Verify solver assigns them to different slots or rooms."""
    
def test_faculty_load_limit():
    """Create faculty with 12 credit limit.
    Verify they're not assigned > 12 credits."""
    
def test_linked_courses_no_overlap():
    """Create BIO101 (parent) and BIO101L (child).
    Verify they're never scheduled at overlapping times."""
```

---

## 9. Dependencies

```toml
[tool.poetry.dependencies]
python = "^3.11"
sqlmodel = "^0.0.22"
fastapi = "^0.115"
uvicorn = "^0.32"
pandas = "^2.2"
networkx = "^3.4"
ortools = "^9.11"
textual = "^0.89"
pyyaml = "^6.0"
pydantic = "^2.9"

[tool.poetry.group.dev.dependencies]
pytest = "^8.3"
pytest-asyncio = "^0.24"
httpx = "^0.27"  # For FastAPI testing
ruff = "^0.7"
```

---

## 10. Key Gotchas

### SQLModel JSON Columns
SQLite can't store Python lists directly. Always use:
```python
my_list: List[str] = Field(default=[], sa_column=Column(JSON))
```

### OR-Tools Variable Explosion
With 100 sections × 50 slots × 30 rooms = 150,000 variables before even adding faculty or students. **Pre-filter aggressively:**
```python
# Only create variables for compatible combinations
if slot.slot_type in course.valid_slot_types:
    if room.room_type in course.required_room_types:
      # Now create the variable
```

### Time Slot Overlap Detection
Don't compute this at solve time. **Precompute** a `Dict[int, Set[int]]` mapping each slot ID to all overlapping slot IDs.

### Floating Point in Objective
OR-Tools CP-SAT uses integers. Multiply urgency scores by 100 and round:
```python
int_urgency = int(urgency_score * 100)
```

### Faculty Approval Lists
An empty `approved_courses` list means "can teach anything" (adjunct pool) vs. a populated list meaning "only these courses."

### Grade Codes — Never Hardcode a Letter-Grade List
`src/database/queries.py` owns `LETTER_GRADE_ORDER` and
`CREDIT_BEARING_NON_LETTER_GRADES` (`TR`/`T`/`P`/`CR`/`S`), and
`doctor.KNOWN_GRADES` is built from them. Real extracts record transfer and
pass/fail credit with non-letter codes; a fresh hardcoded `["A","B",...]` list
silently treats those students as having completed nothing, which fails their
prerequisites. Always import the shared constants.

### Cross-Listed Sections Share One Room
Sections whose courses share a `crosslist_group` are **one physical class**
under several CRNs, and Banner gives each CRN its own capacity. Any new metric
that sums `Section.capacity` must pool by group first (see the pooling block in
`src/core/absorption.py`), or it double-counts the room.

### The Solve Event Stream Is Not Log-Only
`run_generation/run_resolve/run_repair` emit `log`, `done`, `error` **and** the
live-visualization types `viz_init`, `phase`, `incumbent`, `bound`, `activity`,
`solver_line` (`src/core/solver_viz.py`). Any consumer must tolerate unknown
event types and must not assume a `msg` field exists — four tests broke on
exactly that assumption. Non-terminal events are checked against an explicit
allowlist (`STREAM_EVENT_TYPES`) rather than `== "log"`; add new types there.
Note `incumbent.placed` is **optional** (snapshots are throttled): when absent,
keep the last grid rather than clearing it.

### Nothing May Escape Into a Solver Callback
`SolverViz._emit` and `on_log_line` swallow exceptions on purpose. They run on
CP-SAT callback threads during a ten-minute solve, and an exception escaping
there aborts the run — a cosmetic feature must never be able to kill a real
solve. Keep new emit paths wrapped. Related: never scan the room-level `X` dict
from a solution callback (~180k `Value()` calls); use `section_in_slot`
(`(crn, k, slot_id) -> BoolVar`, a few thousand entries) for slot-level data.

### Run Logs Are FERPA-Safe By Construction — Keep Them That Way

`src/core/runlog.py` writes per-run JSONL files (repo `logs/`, overridable
via `GRADIMIZE_LOG_DIR`; `GRADIMIZE_DATA_DIR` is deliberately ignored so
logs can never land inside the FERPA zone) that are the ONLY
channel an AI assistant reads when debugging real-data runs. FERPA-protected
student data — ids (even pseudonyms), grades, per-student attributes,
student↔course rows — must never be written to them. Course codes, rooms,
faculty ids, time slots, and counts/aggregates are allowed. Mechanics:
student-path import errors go through `DataIngestor._add_error_student(...)`
with `describe()` value *shapes*, never values; exception messages on student
paths are dropped (`include_msg=False`) because `int("X")` embeds X in the
message; doctor checks whose details carry student data set `protected=True`
(the run log then records counts only, and `scripts/doctor.py` redacts them
unless `--show-values`). `tests/test_runlog_canary.py` plants ZZ9… sentinels
through import + doctor + the solve tee and sweeps every log file — new
logging that leaks student data fails there. When real data is present,
Claude debugs from `logs/` only: never SELECT the database, never read the
data CSVs. This is mechanically enforced: `.claude/hooks/guard_ferpa.py`
(fail-closed, registered in `.claude/settings.json`) rejects tool calls
referencing the data zone (`/mnt/nvme/gradimize-data`, `$GRADIMIZE_DATA_DIR`),
the repo `data/` tree (except `data/sample/`), `backups/`, roster exports,
or `--show-values`; `.claude/frugal-sensitivity.json` blocks the same from
subagent prompts. To inspect a solve's OUTPUT safely, run
`scripts/run_report.py` — a student-free report (grid, counts, coverage,
utilization) written next to the run logs; its canary is
`tests/test_run_report.py`. Logs must never live inside the data zone —
`runlog_dir()` ignores `GRADIMIZE_DATA_DIR` for exactly that reason.

---

## 11. Glossary

| Term | Definition |
|------|------------|
| **CRN** | Course Reference Number. Unique identifier for a section in Banner. |
| **Section** | A scheduled instance of a course (BIO 101-001, BIO 101-002). |
| **TimeSlot** | A discrete block of time (MWF 8:00-8:50). |
| **Urgency Score** | Priority weight for scheduling a student into a course. |
| **Conflict Matrix** | Course×Course matrix showing which pairs have shared student demand. |
| **Pinned** | A section with manually-fixed time/room/faculty that solver cannot change. |
| **PARENT/CHILD** | Linked lecture and lab sections that must not overlap. |


