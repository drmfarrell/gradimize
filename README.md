# Gradimize

**Course scheduling that optimizes for students graduating on time.**

Gradimize builds a complete semester timetable for a college department or
small institution: which sections run, when and where they meet, who teaches
them, and which students sit in them. It treats scheduling as an optimization
problem whose objective is academic progress, so the students who most need a
course to stay on track are the ones who get seats, and the courses they need
together are kept from colliding.

It runs entirely on your own machine. Nothing is sent to any external service.

## What it does

- **Demand modeling.** Builds the prerequisite graph, finds bottleneck courses,
  and scores every student-course need by urgency: class standing, graduation
  criticality, prerequisite leverage, and whether the student is on track with
  their semester guide. Demand can come from degree-audit exports, observed
  registrations, or the catalog alone.
- **Timetabling with CP-SAT.** Google OR-Tools' constraint solver assigns time
  slots, rooms, and instructors to every section under hard constraints (no
  double-booked rooms or instructors, room types, instructor availability and
  load limits, lecture/lab linkage, travel time between buildings, cross-listed
  sections) and soft preferences (compact teaching days, few preps, preferred
  times, balanced days).
- **Exact student sectioning.** After the timetable is fixed, students are
  seated with a hard no-time-overlap guarantee: an LP relaxation bounds the
  optimum, graduation-critical students are seated exactly, and a CP-SAT pass
  polishes the rest. The reported gap is measured against the LP bound.
- **Repair, don't rebuild.** When an instructor leaves or a room goes offline
  after publication, repair mode moves only what the change forces.
- **Finals scheduling.** A separate solver assigns exam blocks and rooms to
  minimize student exam conflicts under the institution's finals policy.
- **Planning tools.** What-if scenarios (add an instructor, lose a room),
  policy profiles (students-first vs. faculty-first vs. efficiency), enrollment
  stress tests, multi-semester projection with capacity-limited progression,
  per-student advising sheets, and a data-health doctor that validates imports.
- **Three front ends.** A terminal UI (Textual), a web UI (React), and a REST
  API (FastAPI), all over the same engine. Schedules export to Banner-format
  CSV and iCalendar.

Tested scale: about 150 sections and 1,500 students per term, solved in a
few minutes on a desktop. Results are reproducible and benchmarked; see
`benchmarks/RESULTS.md`.

## Quick start

Requirements: Linux (Ubuntu or similar), Python 3.11+ (3.12 recommended),
Node 20+ for the web UI, and `openssl` for the self-signed certificate.

```bash
git clone https://github.com/drmfarrell/gradimize.git
cd gradimize
scripts/bootstrap.sh        # venv, dependencies, web build, sample data
./start.sh                  # terminal UI on the bundled synthetic sample data
./serve.sh --secure         # web UI + API over HTTPS on :8443
```

The sample data under `data/sample/` is entirely synthetic. Replace it with
your own CSV files following `USER_GUIDE.md`, then run
`python scripts/doctor.py` to validate before solving.

## How a solve works

1. **Ingest** catalog, rooms, faculty, students, prerequisites, degree
   requirements, and course history from CSV; time slots and constraints from
   YAML.
2. **Score demand** for every student-course pair and build a conflict matrix
   of course pairs that high-priority students need in the same term.
3. **Place and staff sections.** A fast placement model finds a feasible
   timetable, which warm-starts the full CP-SAT model that optimizes
   urgency-weighted seating minus penalties.
4. **Seat students exactly** on the fixed timetable (LP bound, exact
   graduation-critical seating, CP-SAT polish).
5. **Validate and report**: conflict checks, schedule-quality metrics per
   student and instructor, optimality gap, and exports.

Every knob lives in `config/constraints.yaml` and `config/time_slots.yaml`,
each documented inline.

## Repository layout

```
src/core/      the engine: ingest → demand → solve → section → analyze → export
               (src/core/__init__.py is a module-by-module map)
src/database/  SQLModel tables and the SQLite connection
src/api/       FastAPI backend
src/tui/       Textual terminal UI
web/           React + TypeScript front end
config/        solver configuration (YAML)
scripts/       setup, benchmarking, what-if and projection runners, diagnostics
tests/         pytest suite (560+ tests)
docs/          data-handling procedures, security posture, server setup
```

## Data handling

Gradimize is built for student records. The engine never needs names: a
de-identification script pseudonymizes exports before import and a matching
re-identification step restores them on the way out. Live data can be kept
outside the repository via `GRADIMIZE_DATA_DIR`, logs record shapes rather
than values, and `./bugreport.sh` produces a diagnostic bundle that is safe to
paste. See `docs/DATA_HANDLING_PROCEDURES.md` and `docs/SECURITY_POSTURE.md`.

## Status

Gradimize is a working pilot developed for one department and validated on
synthetic data and back-tests against historical registrations. It has not
yet produced a published schedule for a live term. Expect rough edges around
institution-specific conventions; the configuration is designed to absorb most
of them.

## Documentation

- `USER_GUIDE.md`: data preparation, configuration, every screen and workflow.
- `docs/VM_SETUP.md`: running it as a service on a shared machine.
- `benchmarks/RESULTS.md`: solver benchmarks and the comparison protocol.
- `DESIGN_REVIEW.md` and `CODE_REVIEW.md`: architecture notes and open items.

## License

MIT. See `LICENSE`.
