# Gradimize User Guide

## Overview

Gradimize is an automated university course-scheduling system. It reads your
institution's catalog, student roster, faculty list, rooms, and degree
requirements from CSV files, calculates student demand, and uses a CP-SAT
constraint-programming solver to produce conflict-free schedules that maximise
student access to the courses they need.

---

## Quick Start

```bash
# 1. Clone the repo and enter the directory
cd gradimize

# 2. Create a virtual environment and install
python3 -m venv .venv
.venv/bin/pip install -e .

# 3. Launch the TUI with the bundled sample data
./start.sh tui
```

`start.sh` accepts two modes:

| Command           | What it does                               |
|-------------------|--------------------------------------------|
| `./start.sh tui`  | Load data, then open the interactive TUI   |
| `./start.sh api`  | Load data, then start the REST API on :8000 |

If you omit the mode, `tui` is the default.

Any arguments after the mode are forwarded. For example:

```bash
./start.sh tui --workers 8 --solver-time 120
```

---

## Preparing Your Data

### Directory layout

```
gradimize/
├── config/
│   ├── time_slots.yaml      # time-slot definitions
│   └── constraints.yaml     # hard/soft constraint settings
└── data/
    └── sample/              # ← your data directory
        ├── catalog.csv
        ├── course_history.csv
        ├── degree_requirements.csv
        ├── faculty.csv
        ├── freshmen_projections.csv
        ├── prerequisites.csv
        ├── rooms.csv
        ├── students.csv
        ├── meeting_patterns.csv   (optional)
        └── semester_guide.csv     (optional)
```

Place your CSV files in a directory under `data/`. The default is `data/sample/`.

### Required CSV files

| # | File                       | Purpose                                          |
|---|----------------------------|--------------------------------------------------|
| 1 | `catalog.csv`              | Course catalog — codes, credits, structure, room needs |
| 2 | `course_history.csv`       | Grades students earned in past semesters         |
| 3 | `degree_requirements.csv`  | Which courses each major requires                |
| 4 | `faculty.csv`              | Instructors and the courses they're qualified to teach |
| 5 | `freshmen_projections.csv` | Projected incoming class sizes by major          |
| 6 | `prerequisites.csv`        | Prerequisite and corequisite relationships       |
| 7 | `rooms.csv`                | Rooms, capacities, and equipment                 |
| 8 | `students.csv`             | Student roster with standing and major           |

---

### 1. `catalog.csv`

Defines every course that can be scheduled.

| Column               | Required | Type    | Description                              |
|----------------------|----------|---------|------------------------------------------|
| `code`               | yes      | string  | Unique course code (e.g. `BIOL101`)      |
| `title`              | yes      | string  | Course title                             |
| `credits`            | yes      | int     | Credit hours                             |
| `structure_type`     | yes      | enum    | See **StructureType** table below        |
| `linked_course_code` | no       | string  | Parent course code (for CHILD courses)   |
| `required_room_types`| no       | string  | Comma-separated RoomType values (default: `LECTURE`) |
| `valid_slot_types`   | no       | string  | Comma-separated slot types (default: `MWF_50,TR_75`) |
| `min_enrollment`     | no       | int     | Minimum enrollment to run (default: `5`) |
| `max_class_size`     | no       | int     | Class-size cap for this course (default: the global `section_settings.default_capacity`). Also the ceiling when class sizes float in `room` capacity mode |
| `offer_terms`        | no       | string  | Comma-separated `FALL,SPRING,SUMMER` — terms the course is normally offered (blank = any term) |
| `cadence`            | no       | enum    | `ANNUAL` (default), `BIENNIAL_EVEN` (even years only), `BIENNIAL_ODD` (odd years only) |
| `crosslist_group`    | no       | string  | Shared label (Banner XLST) marking cross-listed courses: members are scheduled as ONE class — same room, time, and instructor, with the room's seats shared across the codes |

**Example rows:**

```csv
code,title,credits,structure_type,linked_course_code,required_room_types,valid_slot_types,min_enrollment,max_class_size
BIOL101,Introduction to Biology,3,PARENT,,LECTURE,"MWF_50,TR_75",10,
BIOL101L,Introduction to Biology Lab,1,CHILD,BIOL101,LAB_BIO,LAB_150,10,24
BIOL201,Cell Biology,3,STANDALONE,,LECTURE,"MWF_50,TR_75",8,
```

**Course cadence — imposed, with an escape valve.** Departments that offer
a course only in, say, even-year springs express that with
`offer_terms=SPRING` + `cadence=BIENNIAL_EVEN`. The optimizer then treats
the cadence as a rule, not a suggestion: in any other term the course gets
**no sections** — *unless* at least `off_cadence_demand_threshold` students
(default 10, in `constraints.yaml`) currently need it, in which case the
optimizer overrides the cadence and logs an **OFF-CADENCE OVERRIDE**
warning naming the course, the demand, and the threshold. Raise the
threshold very high to enforce cadences absolutely. If you don't know the
historical cadences, the Data Browser's **Infer Offerings** (`I`) derives
`offer_terms`/`cadence` from past schedules, and the multi-semester
projector's risk flags show when a cadence is about to strand students.

**Cross-listing.** Give every member of a cross-listed set the same
`crosslist_group` label (e.g. `XL-HIST201` on both `HIST201` and
`AFAM201`). The solver schedules the group as one physical class: paired
sections share their slot, room, and instructor; the room's capacity
applies to their **combined** enrollment; instructors are only assigned if
qualified for every member code (or unrestricted). Members should share
`valid_slot_types`, `required_room_types`, and cadence — the data-health
doctor warns when they don't.

---

### 2. `course_history.csv`

Records of courses each student has completed.

| Column           | Required | Type   | Description                          |
|------------------|----------|--------|--------------------------------------|
| `student_id`     | yes      | string | Must match an `id` in `students.csv` |
| `course_code`    | yes      | string | Must match a `code` in `catalog.csv` |
| `grade`          | yes      | string | Letter grade (e.g. `A`, `B+`, `D`)   |
| `semester`       | yes      | string | Term identifier (e.g. `2024FA`, `2024SP`) |
| `credits_earned` | yes      | int    | Credits earned for this course       |

**Example rows:**

```csv
student_id,course_code,grade,semester,credits_earned
S00001,MATH201,B,2024FA,4
S00001,MATH102,B+,2024SP,3
S00001,MATH101,C+,2024FA,3
```

---

### 3. `degree_requirements.csv`

Maps courses to majors so the system knows what each student needs.

| Column         | Required | Type   | Description                            |
|----------------|----------|--------|----------------------------------------|
| `major`        | yes      | string | Major code (e.g. `BIOL`)              |
| `course_code`  | yes      | string | Required course code                   |
| `concentration`| no       | string | Concentration within the major         |
| `is_required`  | no       | bool   | `True` / `False` (default: `True`)     |
| `category`     | no       | string | Requirement category (default: `core`) |
| `catalog_year` | no       | int    | Catalog year (default: `2024`)         |

**Example rows:**

```csv
major,course_code,is_required,category,catalog_year
BIOL,BIOL101,True,core,2024
BIOL,BIOL101L,True,core,2024
BIOL,BIOL201,True,core,2024
```

---

### 4. `faculty.csv`

Faculty members and the courses they are qualified to teach
(the `approved_courses` column).

| Column             | Required | Type   | Description                                |
|--------------------|----------|--------|--------------------------------------------|
| `id`               | yes      | string | Unique faculty ID                          |
| `name`             | yes      | string | Full name                                  |
| `email`            | yes      | string | Email address                              |
| `max_credits`      | no       | int    | Maximum teaching load in credits (default: `12`) |
| `max_overload_credits` | no   | int    | Approved overload beyond `max_credits` for this person (blank = the blanket `default_overload_credits` in `constraints.yaml`). Only enforced when `faculty_load_limit` is on |
| `approved_courses` | no       | string | Comma-separated course codes. **Blank = unrestricted** (can teach anything) — a course no one lists and no unrestricted instructor covers is UNSTAFFABLE and fails loudly in the pre-flight and doctor |
| `unavailable_slots`| no       | string | Comma-separated time-slot IDs              |
| `preferences`      | no       | JSON   | JSON object with scheduling preferences    |

**Example rows:**

```csv
id,name,email,max_credits,approved_courses,unavailable_slots,preferences
FJOHNSON01,Dr. Angela Johnson,ajohnson@hbcu.edu,12,"BIOL101,BIOL101L,BIOL201",,
FWILLIAMS02,Dr. Marcus Williams,mwilliams@hbcu.edu,12,"BIOL201,BIOL301",,
FSMITH01,Dr. Robert Smith,rsmith@hbcu.edu,12,"CHEM101,CHEM101L,CHEM201",,
```

---

### 5. `freshmen_projections.csv`

Projected incoming freshmen counts per major. Used to forecast demand.

| Column            | Required | Type   | Description                         |
|-------------------|----------|--------|-------------------------------------|
| `major`           | yes      | string | Major code                          |
| `estimated_count` | yes      | int    | Projected number of freshmen (> 0)  |
| `catalog_year`    | yes      | int    | Catalog year for these students     |

**Example rows:**

```csv
major,estimated_count,catalog_year
BIOL,120,2026
CHEM,80,2026
CSCI,150,2026
```

> **Note:** Each major/catalog_year combination must have matching entries in
> `degree_requirements.csv` or validation will fail.

---

### 6. `prerequisites.csv`

Prerequisite and corequisite relationships between courses.

| Column          | Required | Type   | Description                                  |
|-----------------|----------|--------|----------------------------------------------|
| `course_code`   | yes      | string | The course that has the requirement           |
| `prereq_code`   | yes      | string | The prerequisite course code                  |
| `min_grade`     | no       | string | Minimum passing grade (default: `D`)          |
| `is_corequisite`| no       | bool   | `True` if can be taken concurrently (default: `False`) |

**Example rows:**

```csv
course_code,prereq_code,min_grade,is_corequisite
BIOL101L,BIOL101,D,True
BIOL201,BIOL101,C,False
BIOL301,BIOL201,C,False
```

---

### 7. `rooms.csv`

Available rooms and their capabilities.

| Column      | Required | Type   | Description                              |
|-------------|----------|--------|------------------------------------------|
| `id`        | yes      | string | Unique room ID (e.g. `SCI-101`)         |
| `room_type` | yes      | enum   | See **RoomType** table below             |
| `building`  | yes      | string | Building name                            |
| `number`    | yes      | int    | Room number                              |
| `capacity`  | yes      | int    | Seat count                               |
| `equipment` | no       | string | Comma-separated equipment list           |
| `is_active` | no       | bool   | Whether room is available (default: `True`) |

**Example rows:**

```csv
id,building,number,capacity,room_type,equipment,is_active
SCI-101,Science Building,101,35,LECTURE,"projector,whiteboard",True
SCI-102,Science Building,102,35,LECTURE,"projector,whiteboard",True
SCI-201,Science Building,201,25,LAB_BIO,"microscopes,fume_hood",True
```

---

### 8. `students.csv`

Current student roster.

| Column           | Required | Type   | Description                           |
|------------------|----------|--------|---------------------------------------|
| `id`             | yes      | string | Unique student ID                     |
| `name`           | yes      | string | Full name                             |
| `email`          | yes      | string | Email address                         |
| `class_standing` | yes      | enum   | See **ClassStanding** table below     |
| `credits_earned` | yes      | int    | Total credits earned                  |
| `major`          | yes      | string | Major code                            |
| `minor`          | no       | string | Minor code                            |
| `concentration`  | no       | string | Concentration within major            |
| `is_honors`      | no       | bool   | `True` / `False` (default: `False`)   |
| `is_athlete`     | no       | bool   | `True` / `False` (default: `False`)   |
| `catalog_year`   | no       | int    | Student's catalog year (default: `2024`) |
| `pacing_preference` | no    | enum   | `cluster` (few days, tight packing — commuters/workers) or `spread` (breathing room, no marathons); blank = balanced. Used by the seat rebalancer (`r`); collected via advising, carries no identifying information |

**Example rows:**

```csv
id,name,email,class_standing,credits_earned,major,minor,is_honors,is_athlete,catalog_year
S00001,Linda Smith,linda.smith1@student.hbcu.edu,SR,107,MATH,MATH,False,True,2022
S00002,Aaliyah Johnson,aaliyah.johnson2@student.hbcu.edu,JR,60,CHEM,MATH,False,False,2023
S00003,Jalen Wilson,jalen.wilson3@student.hbcu.edu,SR,104,BUSN,ENGL,False,True,2023
```

---

### Optional files

#### `meeting_patterns.csv`

Override the default slot/room assignments for courses with non-standard
meeting patterns (e.g. a 2-hour Tuesday lecture + a 6-hour Thursday lab).

| Column               | Required | Type   | Description                          |
|----------------------|----------|--------|--------------------------------------|
| `course_code`        | yes      | string | Must match a `code` in `catalog.csv` |
| `meeting_seq`        | yes      | int    | Sequence number (0, 1, 2, ...)       |
| `valid_slot_types`   | yes      | string | Comma-separated slot types           |
| `required_room_types`| yes      | string | Comma-separated room types           |

**Example:**

```csv
course_code,meeting_seq,valid_slot_types,required_room_types
BMED3301,0,T_120,LECTURE
BMED3301,1,R_360,LAB_BIO
```

#### `semester_guide.csv`

Defines the recommended semester-by-semester course sequence for each major.

| Column            | Required | Type   | Description                            |
|-------------------|----------|--------|----------------------------------------|
| `major`           | yes      | string | Major code                             |
| `catalog_year`    | yes      | int    | Catalog year                           |
| `semester_number` | yes      | int    | Semester sequence (1, 2, 3, ...)       |
| `concentration`   | no       | string | Concentration within major             |
| `course_code`     | no       | string | Specific course (or use `category`)    |
| `category`        | no       | string | Generic category (e.g. `elective`)     |
| `credits`         | no       | int    | Credits (default: `3`)                 |
| `label`           | no       | string | Display label (defaults to course code or category) |

> Each row must have at least one of `course_code` or `category`.

---

### Enum reference

#### StructureType

| Value        | Meaning                                           |
|--------------|---------------------------------------------------|
| `STANDALONE` | Normal course with no linking requirements         |
| `COMBINED`   | Lecture + lab in a single CRN                      |
| `PARENT`     | Lecture component with linked lab sections          |
| `CHILD`      | Lab component linked to a PARENT course            |

#### RoomType

| Value       | Meaning              |
|-------------|----------------------|
| `LECTURE`   | Standard lecture room |
| `LAB_WET`   | Wet lab              |
| `LAB_DRY`   | Dry / computer lab   |
| `LAB_BIO`   | Biology lab          |
| `LAB_CHEM`  | Chemistry lab        |
| `SEMINAR`   | Seminar room         |
| `AUDITORIUM`| Large auditorium     |

#### ClassStanding

| Value | Meaning   |
|-------|-----------|
| `FR`  | Freshman  |
| `SO`  | Sophomore |
| `JR`  | Junior    |
| `SR`  | Senior    |

---

## Configuration

Both configuration files live in the `config/` directory.

### `time_slots.yaml`

Defines the types of time slots and the concrete slots available for
scheduling.

**Slot types section** — each entry defines a meeting pattern:

```yaml
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

  LAB_150:
    pattern: "single"          # one slot per listed day
    days: ["M", "T", "W", "R", "F"]
    duration: 150
```

**Slots section** — each entry creates a concrete bookable slot:

```yaml
slots:
  # MWF 50-minute blocks
  - type: MWF_50
    start: "08:30"
  - type: MWF_50
    start: "09:30"
  # ...

  # TR 75-minute blocks
  - type: TR_75
    start: "08:30"
  - type: TR_75
    start: "10:00"
  # ...

  # Lab blocks — can override days for single-day labs
  - type: LAB_150
    days: ["M"]
    start: "08:30"
  - type: LAB_150
    days: ["M"]
    start: "11:30"
  # ...
```

The bundled config provides:

- 7 MWF 50-minute slots (08:30 – 16:30, skipping 12:30 – 14:30 for Faculty Common Hour)
- 6 TR 75-minute slots
- 5 MW 75-minute slots
- 15 lab slots (3 per day, Mon – Fri)

### `constraints.yaml`

Controls the hard and soft constraints the solver enforces.

**Hard constraints** (must always be satisfied):

```yaml
hard_constraints:
  room_no_overlap: true          # no double-booking rooms
  faculty_no_overlap: true       # no double-booking faculty
  respect_room_capacity: true    # enrollment <= assigned room's capacity
  match_room_type: true          # course must match room capability
  faculty_approval_required: true # faculty can only teach approved courses
  linked_no_overlap: true        # parent/child sections can't overlap
  min_travel_time: 10            # minutes between buildings (0 disables)
  graduation_critical_required: true  # seniors MUST get required courses
```

Student schedule overlap is intentionally *not* a constraint of the joint
model (per-student overlap clauses explode at scale). There, course pairs with
high combined student urgency are kept apart via soft penalties, gated by
`conflict_urgency_threshold`. Once the timetable is fixed, the exact sectioning
pass (see "Exact student sectioning (phase B)" below) enforces per-student
no-overlap exactly.

**Soft constraints** (preferences, weighted in the objective function):

```yaml
soft_constraints:
  early_morning_penalty:
    hour: 8
    weight: -10
  late_afternoon_penalty:
    hour: 17
    weight: -10
  back_to_back_penalty:      # per adjacent faculty slot pair;
    faculty:                 # faculty with allow_back_to_back opt out
      weight: -20
  faculty_preference_bonus:
    weight: 5
  day_balance_penalty:
    weight: -15
```

**Blocked periods** — time ranges where no classes may be scheduled:

```yaml
blocked_periods:
  - name: "Faculty Common Hour"
    days: ["M", "W", "F"]
    start_time: "12:30"
    end_time: "14:30"
    reason: "Faculty meetings and office hours"
```

**Conflict urgency threshold** — when the combined urgency of students who
need both courses exceeds this value, the pair gets a hard no-overlap
constraint:

```yaml
conflict_urgency_threshold: 50.0
```

**Solver settings:**

```yaml
solver:
  max_time_seconds: 300
  num_workers: 4
  log_search_progress: false
  exact_sectioning: true          # phase B, see below
  sectioning_time_seconds: 120
  sectioning_lp: true
  symmetry_breaking: true
  no_overlap_formulation: cliques # cliques | pairwise
  # linearization_level: 1        # 0-2; unset = CP-SAT default
```

Three further settings are tuning knobs you rarely need to touch:
`symmetry_breaking` (default `true`) orders interchangeable sections of one
course to prune redundant search; `no_overlap_formulation` (default `cliques`)
picks the room/faculty no-overlap encoding, with `pairwise` as the older
fallback; and `linearization_level` (default unset, meaning the CP-SAT default;
integer 0-2) trades model strength against speed.

**Exact student sectioning (phase B)** — after the joint model fixes the
timetable, a second solve re-seats every student with a hard rule that no
student is in two overlapping sections. It also respects section capacity and
cross-list pack capacity, keeps graduation-critical seats hard (with a soft
fallback if that is infeasible), and maximizes urgency-weighted seating. The
joint model's seats are its warm start. Phase B runs four stages: (1) an LP
relaxation (GLOP, ~15 s) whose bound sits about 0.016% above the true integer
optimum; (2) exact seating of graduation-critical demands with a tiny CP-SAT
model, preferring the LP's choices; (3) deterministic greedy seatings (from the
joint model's seats, from scratch, and by rounding the LP) around those fixed
critical seats; (4) a CP-SAT polish from the best seating, kept only if it beats
it. Every stage yields a complete collision-free seating, so the pass never
fails. On the sample instance (156 sections, 15,381 demands) stages 1-3 alone
reach 4,312 seats within 1.0% of the LP bound in 28 s with 0 unmet
graduation-critical demands; a 120 s polish reaches about 1.6-0.9%, and 600 s
reaches 0.2% of optimal (the proven optimum is 4,318 seats). Knobs:
`exact_sectioning` (default `true`), `sectioning_time_seconds` (polish budget,
default `120`; `0` = no polish, stages 1-3 only; 600 is worthwhile for a final
production run) and `sectioning_lp` (default `true`, toggles the LP stage). The
reported `gap_pct` is measured against the LP bound, so it is an honest distance
from the true optimum. The
schedule's student-conflict count should now be 0; any non-zero count means
phase B was disabled or failed, so check the log (its stats are also in
`result.stats["sectioning"]`: `method`, seats, overlaps removed, unmet
graduation-critical demands).

**Section sizing** — how many sections a course gets and how big they are:

```yaml
section_settings:
  default_capacity: 30       # seats per section in "fixed" mode
  students_per_section: 25   # one extra section per this many demands
  max_sections_per_course: 5
  crn_start: 10001
  capacity_mode: fixed       # fixed | room
  max_class_size:            # global ceiling in room mode (blank = none)
```

`capacity_mode` controls class size. In `fixed` mode (the default) every
generated section seats `default_capacity`, unless the course sets its own
`max_class_size` in `catalog.csv`. In `room` mode class sizes **float**: the
solver may fill a section up to its assigned room's capacity, and the saved
section capacity is whatever the chosen room allows — so a 60-seat hall
yields a 60-seat section while a 18-seat seminar room yields 18. Caps still
win: a course's `max_class_size` (or the global one) is a hard ceiling on how
far a section can grow. Both knobs can also be set per run in the TUI
Settings screen or via the web generate call.

---

## Running the TUI

Launch with:

```bash
./start.sh tui
```

### Global keybindings

These keys work from most screens:

| Key | Action                |
|-----|-----------------------|
| `d` | Switch to Dashboard   |
| `s` | Switch to Schedule    |
| `c` | Switch to Conflicts   |
| `b` | Switch to Data Browser|
| `w` | Switch to Settings    |
| `q` | Quit                  |

### Dashboard screen

| Key | Action                          |
|-----|---------------------------------|
| `g` | Generate a new schedule         |
| `h` | Run data-health checks (doctor) |

The health check vets the loaded data before you solve: referential
integrity (history/demands pointing at real students and courses),
solvability (courses with no compatible room or slot, staffing
bottlenecks under teaching-qualification rules), and quality (duplicates,
unrecognized grades, standing-vs-credits mismatches). Results are graded
OK / WARN / FAIL in the log panel — **fix every FAIL before generating**.
One solvability check deserves a callout: **prereq-chain-vs-cadence**
walks every prerequisite chain against each course's offering pattern
(`offer_terms` + biennial cadence, from both even- and odd-year entry
points) and FAILS if a *required* course cannot be completed within 8
regular semesters — a chain like "BIOL401 sits behind two biennial links"
is a 4-year-graduation blocker that no amount of clever scheduling can
fix, only a cadence change can.
The same checks run from the command line (`python scripts/doctor.py`,
exit code 2 on failures, `--json` for scripting) and over the API
(`GET /api/data/health`). Run it after every data import — it is the
"validate a small sample" step of the data-request workflow.

### Schedule screen

| Key | Action                          |
|-----|---------------------------------|
| `n` | Next schedule                   |
| `p` | Previous schedule               |
| `e` | Export CSV                      |
| `x` | Export Excel                    |
| `a` | Export Banner CSV               |
| `i` | iCal export (.ics calendars)    |
| `v` | Section viability report        |
| `u` | Student schedule-quality report |
| `r` | Rebalance seats (pacing-aware)  |

Section viability (`v`, also the **Section viability** button in the web
schedule view and `GET /api/schedule/{id}/viability`) lists every section
below its course's `min_enrollment` and answers the cancel-or-run question
honestly: how many enrolled students could reseat into sibling sections
(counting free seats AND checking their personal schedules for time
conflicts, graduation-critical students first), who would be stranded, and
— using the course's cadence — when the next offering would even exist.
Recommendations range from "Empty section — safe to cancel" to "Cancelling
strands 2 students, 1 graduation-critical; next offering 2028SP — do not
cancel without an advising review."

Student schedule quality (`u`, also the **Student quality** button in the
web schedule view and `GET /api/schedule/{id}/student-quality`) asks a
different question: a schedule with zero hard conflicts can still hand a
student a week nobody would choose. For every enrolled student it expands
their meetings into a day-by-day picture and reports how many days they're
on campus, how much idle time sits between classes (gaps over 15 minutes),
their single longest gap, their longest unbroken class marathon, their
earliest start and latest end — plus two conflict measures whose
definitions follow Purdue's open-source UniTime system: **lunch-crunched
days** (classes leave no 30-minute break anywhere between 11:00 and
13:00) and **long workdays** (more than 6 hours from first class start to
last class end). A distribution summary covers the whole
schedule (mean days-on-campus, mean gap, how many students have a 5-day
spread, a 3-hour-plus single gap, 3+ marathons over 3 hours, no lunch
break, or a 6-hour-plus workday), and the
worst 20 students are ranked by a simple badness score that weighs day-
spread most heavily. The report also breaks down, per department (the
alphabetic prefix of the course code), what share of its section-meetings
land in "prime time" (10:00-14:00) — a quick fairness check on who's
getting the slots everyone wants.

iCal export (`i`) writes one `.ics` calendar file per instructor and per
room into `exports/ical/schedule_<id>/` — each imports directly into
Outlook, Google Calendar, or Apple Calendar as a weekly repeating schedule
for the term. Hand an instructor their file and their teaching schedule is
on their phone. Per-person files are also available from the API
(`GET /api/schedule/{id}/ical/instructor/{faculty_id}`, `…/ical/room/{room_id}`,
`…/ical/student/{student_id}` — the student form covers advising sheets).
Term start/end dates are approximated from the semester code (fall ≈
Aug 25–Dec 12, spring ≈ Jan 20–May 8); the module accepts exact academic-
calendar dates when you have them.

Seat rebalancing (`r`, also `POST /api/schedule/{id}/rebalance?mode=…`)
acts on what the quality report reveals. The timetable and every section's
capacity stay exactly as solved; only *which student sits in which sibling
section* changes. Each student is re-seated to fit their week shape —
their own `pacing_preference` (`cluster`: fewest days, tight packing;
`spread`: no marathons, gaps welcome) or the balanced default — and every
move is safe by construction: only into a free seat, never creating a new
time conflict, never changing anyone's course list. Run `u` again
afterward to see the improvement. Deterministic, seconds to run, and
re-runnable after add/drop churn.

### Conflicts screen

| Key | Action                          |
|-----|---------------------------------|
| `r` | Refresh conflict data           |

### Data Browser screen

| Key | Action                          |
|-----|---------------------------------|
| `1` | View Students table             |
| `2` | View Courses table              |
| `3` | View Faculty table              |
| `4` | View Rooms table                |

### Settings screen

| Key       | Action                          |
|-----------|---------------------------------|
| `1`       | Edit solver workers count       |
| `2`       | Edit solver time limit          |
| `3`       | Edit target semester            |
| `4`       | Edit class-size cap (0 = config default) |
| `5`       | Cycle capacity mode: config default → fixed → room |
| `6`       | Edit conflict threshold (blank = config default; lower = stricter about student conflicts — small cohorts can afford lower values) |
| `Enter`   | Confirm edited value            |
| `Escape`  | Cancel editing                  |

---

### Working from the TUI

The TUI now covers the full working loop without the web UI:

- **Import data** — press `M` from any screen. Point it at a folder
  (default: `$GRADIMIZE_DATA_DIR/incoming`, else `data/incoming`), drop
  extract files in under their canonical names (`students.csv`,
  `catalog.csv`, …), press `R` to rescan and `I` to import everything
  found, in dependency order. Same dry-run-protected importer as the web
  upload; every import writes a run log.
- **Pin / unpin** — highlight a section on the Schedule screen, press `T`.
  Pinned sections keep their time/room/instructor on re-solves.
- **Re-solve** (`O`) and **Repair** (`K`) — start from the Schedule
  screen; the app switches to the Dashboard so the run gets the live
  solver visualization and log. Repair prints each forced move
  (MOVED/DROPPED lines).
- **Diff** (`Y`) — compare the shown schedule against the next-older one.
- **Conflict check** (`Z`) — room/faculty double-bookings and student
  clashes in the shown schedule.
- **Explain** (`Enter` on a row) — why the solver placed that section
  where it did.
- **Suggestions** (`J`) — improvement recommendations for the shown
  schedule (faculty overloads, imbalances, utilization), most important
  first.
- **Projections** — Data Browser view `5` lists freshmen projections
  (`X` deletes the highlighted one; create them by importing
  `freshmen_projections.csv`). Settings option `7` toggles whether
  Generate folds projected freshmen demand into the solve (off by
  default).

## CLI Flags

The TUI accepts these optional flags (pass them after the mode):

```bash
./start.sh tui --workers 8 --solver-time 120
```

| Flag            | Type | Default | Description                        |
|-----------------|------|---------|------------------------------------|
| `--workers`     | int  | 4       | Number of parallel solver workers  |
| `--solver-time` | int  | 60      | Solver time limit in seconds       |
| `--no-viz`      | flag | off     | Disable the live solver visualization (on by default; useful for scripted/headless runs or a slow terminal) |

---

## Multi-semester projection

Project the next several terms — not just the one you're about to publish:

```bash
python scripts/project_terms.py --start 2026FA --terms 6 --time-limit 180
```

For each regular term (fall/spring) the projector recomputes demand from
every student's simulated progress, runs the **full solver**, then advances
the world *capacity-limited*: students complete only the courses they got
seats in, credits and standings update, finished students graduate out, and
each fall the projected freshman cohort (Freshmen Projections screen)
enters. Prerequisite chains and biennial course cadences drive what each
term can offer.

Everything runs against a **throwaway copy** of the database — live data is
never touched. The output is a markdown report with per-term summaries, a
course-by-term offering matrix, and risk flags such as *"BIOL301 is
off-cadence this term but 14 students need it"* or a future term whose
staffing can't work. Budget roughly `--time-limit` seconds per term of wall
time. Treat later terms as planning signals, not commitments — and run the
normal generator for the term you actually publish.

---

## Repairing a published schedule

Something always breaks after publication: an instructor resigns, a room
goes offline for maintenance, an approval changes. Running the generator
again would hand you a brand-new schedule — technically better, practically
useless, because hundreds of people already planned around the published
one.

**Repair mode** re-solves against your *current* data while treating every
published assignment as strongly sticky. Only changes the disruption
actually forces will happen; everything else keeps its time slot, room,
instructor, and student roster. The result is a **new** schedule version
(the original is untouched) plus a report of exactly what moved — the same
diff you get from comparing any two versions.

Typical workflow:

1. Update the data to reflect reality (mark the instructor unavailable,
   deactivate the room, approve a substitute instructor for the course).
2. In the web UI, open the schedule → **Re-solve / Repair** → choose
   **Repair (minimal changes only)** → Start. Or via the API:
   `GET /schedule/{id}/repair/stream` (same SSE protocol as generate).
3. Read the repair result: *"41 sections untouched, 2 changed"* — each move
   is listed field-by-field (`instructor Dr. Smith → Dr. Jones`). Moves are
   also narrated as `MOVED …` lines in the solver log.

Repair honors pinned sections as hard constraints, exactly like re-solve.
If a repair reports more movement than you expected, the disruption really
did cascade (e.g. the only qualified substitute is already teaching in that
slot) — the diff shows the chain.

The stickiness weights live in `constraints.yaml` under
`soft_constraints.repair_anchor` (time moves cost the most, then instructor
changes, then room moves; a small `seat` weight keeps students in their
planned sections on ties). The defaults are deliberately heavy — you should
rarely need to touch them.

**Repair vs. Re-solve:** *Re-solve* re-optimizes everything that isn't
pinned — use it while drafting. *Repair* moves as little as possible — use
it after people have seen the schedule.

*(Minimal-change re-solving is a well-established timetabling technique —
"minimal perturbation" scheduling has shipped in Purdue's open-source
UniTime system since the mid-2000s (Müller & Rudová, PATAT 2004); Gradimize
implements the same idea on its CP-SAT engine.)*

---

## Advisor mode

Tools for the advising desk, answered from the published schedule in
milliseconds (no solver run):

**Student schedule sheet** — `GET /api/schedule/{id}/student/{student_id}/sheet`
returns a printable markdown handout: the student's course table (CRN,
credits, days, time, room, instructor), a day-by-day *week at a glance*,
total credits, and a pointer to their phone-importable calendar file
(`…/ical/student/{student_id}`). Print it for the appointment or paste it
into an email.

**"Why can't it be at 10am?"** —
`GET /api/schedule/{id}/sections/{section_id}/explain-move?slot_id=N`
answers the counterfactual every scheduler gets asked. It checks the
proposed slot against the schedule as-published and reports **blockers**
(course's slot types don't allow it; it lands in a blocked period like the
faculty common hour; the instructor is unavailable or already teaching
then; every compatible room is occupied or too small) and **costs**
(how many enrolled students have another class at that time, and how many
of those seats are graduation-critical), plus which rooms *would* be free.
The one-line `verdict` ranges from "Freely movable" through "Possible, at
a price: 4 student conflicts" to "Cannot move: Dr. Smith already teaches
MATH201-001 at that time." It analyzes today's schedule statically — for
a change you actually want to make, pin the section where you want it and
run **Repair** to let the solver reconcile the rest.

**One-student replanner** — `GET /api/data/students/{id}/replan?start=2026FA`
answers "failed MATH102 — what's the best path now?" It simulates that one
student term-by-term against the catalog's offering patterns: each term
takes their most urgent still-available courses (prerequisites enforced,
`offer_terms` and biennial cadences respected) up to a credit load
(`&credits=15`), marks them complete, and advances — reporting the
projected graduation term, the term-by-term course plan as a printable
markdown handout, and cadence warnings ("BIOL301 not offered again until
2028SP — a missed term costs a year"). Failed and withdrawn courses count
as *not completed* (completion = grade D or better), so retakes are
scheduled automatically before anything that depends on them. Runs on a
throwaway database copy in under a second; seats are assumed available —
it plans against the *catalog*, so cross-check big claims against the
term's actual schedule.

**Bulk student-schedule export** — the sheet and calendar above are
one-student-at-a-time tools for the advising desk. To hand every enrolled
student their schedule at once (a mailing, not an appointment), run:

```bash
python scripts/export_student_schedules.py --schedule-id 1 --out exports/dist
```

This writes `student_schedules.csv` (one row per enrollment × meeting —
course, CRN, section, days, time, room, instructor; a section with no
meetings gets one row with blank meeting fields), plus, unless suppressed
with `--no-sheets` / `--no-ical`, a `sheets/<student_id>.md` advisor sheet
and an `ical/<student_id>.ics` calendar for every enrolled student. All
output is keyed by the pseudonymous `student_id` this server stores — the
registrar-side `reidentify.py` tool swaps pseudonyms back to real IDs on
their machine before anything is actually mailed out.

---

## Policy profiles

Every soft-constraint weight is a policy choice: protecting students from
conflicts and 8am sections trades against compact faculty teaching weeks
trades against running fewer, fuller sections. Instead of arguing about
weights in the abstract, solve the same term under each named policy and
compare:

```bash
python scripts/compare_profiles.py --semester 2026FA --time-limit 120
```

Profiles shipped: **balanced** (the YAML defaults), **students_first**
(conflict threshold 20, heavy early/late penalties, faculty-convenience
weights near zero), **faculty_first** (few preps, compact teaching days,
preferences honored), **efficiency_first** (section closure ON with a hard
consolidation push, relaxed conflict threshold). Each profile solves on its
own throwaway database copy — same data, same time budget — and the report
is a Pareto trade-off table grouped by stakeholder: enrollment and conflict
numbers, mean days-on-campus and idle gaps (Students); mean preps and
teaching days (Faculty); sections run/closed, seat fill, room utilization
(University). Pick the column whose trade-offs you can defend, then adopt
those weights in `constraints.yaml` for the real run. Budget one
`--time-limit` per profile. (The same machinery answers resource questions
— "what if we hire another chemist?" — via `scripts/what_if.py`.)

## Enrollment stress test

Freshman projections are estimates; the schedule shouldn't shatter when
they're wrong. The stress test re-solves the term with the projected
freshman class deterministically scaled to −20%, −10%, +10%, and +20% of
the estimate (each on its own throwaway database copy) and reports where
the schedule bends:

```bash
python scripts/stress_test.py --semester 2026FA --time-limit 60
```

The report leads with the executive summary ("holds from −20% to +10%; at
+20% CHEM101 overflows by 12 seats"), then a side-by-side variant table
and two fragility lists: **fragile-low** courses (a smaller class drops a
section below its `min_enrollment` — a cancellation risk), and
**fragile-high** courses (a bigger class leaves students without seats —
an add-a-section risk). A variant that fails to solve at all is itself the
headline finding: the +20% class literally does not fit the current room
and instructor supply. Only *projected freshman* demand varies; returning-
student demand is held constant. Budget one `--time-limit` per factor.

---

## Deriving faculty teaching history

Historical Banner schedule extracts (`schedule_*.csv`, see
`exports/Gradimize_Data_Request.md` §6/§7) record who actually taught what,
when. This importer turns that into SEED data for two `Faculty` fields:

```bash
python scripts/derive_teaching_history.py path/to/schedule_csvs/ [--overwrite] [--dry-run]
```

For each `INSTRUCTOR_ID` across all provided terms it derives:

- **`approved_courses`** — every course code (SUBJECT+COURSE_NUMBER, e.g.
  `BIOL201`) the instructor is on record teaching.
- **`preferences["preferred_slots"]`** — the instructor's most common
  meeting pattern/time, mapped to real `TimeSlot` IDs. This is the *only*
  preference key the solver's faculty-preference bonus reads
  (`src/core/penalties.py`), so it's the only key derivation writes into
  the solve. A `preferences["derived_history"]` summary (course/term
  counts, day-pattern frequencies, morning/afternoon share) rides along
  for human review only — the solver never reads it.

**Fill-only by default.** `approved_courses` is set only when currently
empty; `preferred_slots`/`derived_history` are set only when the key is
absent. `--overwrite` replaces both regardless. `--dry-run` prints the
per-faculty before→after report without committing. Instructor IDs not
already in the `Faculty` table are reported, never auto-created.

**Read this before running it against live data:** an *empty*
`approved_courses` list means "can teach anything" to the solver. Filling
it in **narrows** what that instructor can be assigned — it is not purely
additive. Departments must review the derived list and add any course the
instructor could/should teach but hasn't recently before this data is
trusted for a live solve. The CLI prints this warning on every run.

---

## Absorption test (Fall 2026 back-test, experiment B)

The multi-semester projection and stress test both ask "what should the
schedule be." The absorption test asks a narrower question: given a
schedule that's already FIXED — every section's meeting time, room, and
instructor locked, nothing moves — how much of the CURRENT demand can it
actually seat? Score two schedules side by side (the manually-built Banner
import and a Gradimize-generated one) against the same demand set (real
registrations, freshmen included) to compare timetables on equal footing:

```bash
python scripts/absorption_test.py --schedule-id 1 --schedule-id 2 \
    --report-md exports/absorption_2026FA.md
```

Seating is a deterministic, urgency-descending greedy pass — not a
re-solve — because the timetable is already fixed and the only free
variable is who sits in which seat. It never writes to the database:
`StudentDemand`, `SectionEnrollment`, and the schedules themselves are
read-only inputs. The report shows demand seated / total, graduation-
critical seated, freshman (FR-standing) seated, distinct students fully
seated (every course they need), total seats offered vs. total demand,
and the top unmet courses per schedule. Sections whose courses share a
`crosslist_group` are pooled into one seat counter (capped by the shared
room when known), so a cross-listed pair counts as one physical class
instead of double-counting the room. A schedule imported from Banner
may contain sections whose meeting time didn't match any `TimeSlot` at
import — those are still seatable by capacity but their conflict status
is unknown (`unplaced_sections` in the report), so treat seated % as an
upper bound on rows where that count is nonzero.

## Running the API

```bash
./start.sh api
```

This starts a REST API server at `http://127.0.0.1:8000`.

### Key endpoint groups

| Prefix             | Purpose                                      |
|--------------------|----------------------------------------------|
| `/schedule/`       | Generate, list, export, pin/unpin, re-solve, repair |
| `/data/`           | Browse courses, students, faculty, rooms, demands |
| `/conflicts/`      | Query high-conflict course pairs             |
| `/projections/`    | Manage freshmen enrollment projections       |
| `/guides/`         | Semester guide CRUD                          |
| `/recommendations/`| Get improvement suggestions for a schedule   |
| `/health`          | Health check                                 |

### Common workflows

**Generate a schedule:**

```
POST /schedule/generate
```

**List all schedules:**

```
GET /schedule/
```

**Export a schedule to Banner CSV:**

```
GET /schedule/{id}/export/banner
```

**Import an existing Banner schedule:**

```
POST /schedule/import
```

---

## Run logs (safe to share)

Every import, health check, and solve writes a run log:
`logs/run-<timestamp>-<kind>.jsonl` in the repo (never inside the data
directory — see the isolation section below; `$GRADIMIZE_LOG_DIR` overrides
the location). Run logs
are student-data-free **by construction** — they record row numbers, column
names, value *shapes* (e.g. "text(medium, digits+punct=/)"), counts, and
solver statistics, but never student ids, names, or grades. That makes them
safe to paste into email or an AI assistant when troubleshooting a failed
import or a bad solve.

`python scripts/doctor.py` follows the same rule: student-level examples are
redacted by default (the printed output is safe to share); rerun with
`--show-values` to see actual ids/grades on your own terminal. The web
dashboard always shows full details — it is the operator's own screen.

The newest ~60 run files are kept per directory; older ones are pruned
automatically.

---

## Real-data isolation & run analysis

Once real (de-identified) extracts arrive, the working rule is:
**data never in the repo, logs never in the data dir, and the AI assistant
reads only logs and reports — never the data.**

### Layout

```
/mnt/nvme/gradimize-data/        ← FERPA zone (extracts, gradimize.db, backups)
  incoming/                      ← drop delivered CSVs here
~/gradimize/                     ← the repo: code only
  logs/                          ← safe zone: run-*.jsonl + report-*.md
```

Set the environment once (shell profile or the service unit):

```bash
export GRADIMIZE_DATA_DIR=/mnt/nvme/gradimize-data
```

The log rule needs no configuration: run logs always resolve to the repo's
gitignored `logs/`, never into the data directory (set `GRADIMIZE_LOG_DIR`
only to move them somewhere else that is also outside the zone).

### Workflow for a data delivery

1. Copy the extracts into `/mnt/nvme/gradimize-data/incoming/`.
2. Start `./serve.sh --secure` and upload each file through the web UI
   (dependency order: time slots → rooms → catalog → prerequisites →
   faculty → students → history → registrations). Every upload writes a
   run log.
3. Run `python scripts/doctor.py` — output is redacted by default (safe to
   paste anywhere); use `--show-values` on your own terminal when you need
   the actual ids. Fix every FAIL, re-upload, repeat.
4. Generate the schedule. The whole solve is captured in a run log.
5. Run `python scripts/run_report.py` — writes a student-free report
   (section grid, enrollment counts, demand coverage, utilization,
   aggregate quality metrics) next to the run logs.

The assistant analyzes a run from exactly three artifacts: the run logs,
the report, and redacted doctor output. When those aren't enough, it writes
a script for **you** to run, and you relay what matters.

### Enforcement layers

1. **Layout + git**: the data zone is outside the repo; `.gitignore` makes
   `data/` uncommittable without `-f`.
2. **Tool guard**: `.claude/hooks/guard_ferpa.py` (registered in
   `.claude/settings.json`, fail-closed) mechanically rejects any assistant
   tool call that references the data zone, the repo `data/` tree (except
   `data/sample/`), `backups/`, roster exports (`exports/*.csv|xlsx`), or
   `--show-values`. `.claude/frugal-sensitivity.json` blocks the same
   material from ever being handed to a subagent.
3. **Unix permissions (optional, strongest)**: run the server as a separate
   user so the assistant's account simply cannot open the data zone:

   ```bash
   sudo useradd -r -M gradimize-svc
   sudo chown -R gradimize-svc:gradimize-svc /mnt/nvme/gradimize-data
   sudo chmod 700 /mnt/nvme/gradimize-data
   sudo -u gradimize-svc GRADIMIZE_DATA_DIR=/mnt/nvme/gradimize-data \
        GRADIMIZE_TOKEN=... ./serve.sh --secure
   ```

   Note the service user also needs write access to `logs/` and the web
   build; iron out those details when the data actually lands. Layers 1–2
   stop accidents; layer 3 is the locked door.

---

## Troubleshooting

### "Virtual environment not found"

`start.sh` expects a `.venv` directory at the project root. Create one:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Missing or misnamed CSV files

The loader expects exact filenames (`catalog.csv`, not `courses.csv`). If a
required file is missing you will see a `FileNotFoundError`. Double-check that
all 8 files are present in your data directory.

### Validation errors on load

Common causes:

- **Invalid enum values** — `structure_type`, `room_type`, and
  `class_standing` must use the exact values listed in the enum tables above.
- **Invalid `preferences` JSON** — the `preferences` column in `faculty.csv`
  must be valid JSON or left empty.
- **Freshmen projection without degree requirements** — every
  `major`/`catalog_year` pair in `freshmen_projections.csv` must have matching
  rows in `degree_requirements.csv`.
- **Estimated count must be > 0** — zero or negative values in
  `freshmen_projections.csv` are rejected.
- **Unknown course in meeting patterns** — every `course_code` in
  `meeting_patterns.csv` must exist in `catalog.csv`.
- **Semester guide row missing both course_code and category** — each row needs
  at least one of the two.

### Terminal display issues

If the TUI renders incorrectly:

- Ensure your terminal supports 256 colors or true color.
- Try a different terminal emulator (the TUI is built with
  [Textual](https://textual.textualize.io/) and works best in modern
  terminals).
- On SSH sessions, make sure `TERM` is set correctly
  (e.g. `export TERM=xterm-256color`).
