"""Gradimize core: the domain engine behind the TUI and the web API.

Both front ends are thin — the TUI calls into this package directly, the web
API calls it through ``src/api/routes`` — so everything that decides *what a
schedule is* lives here. The package is a layered DAG (no import cycles); read
it top to bottom and each layer only depends on the ones above it.

Where to look
-------------
**Foundation** — small shared helpers, no other core deps:
    timeutil        time/slot arithmetic (parse, overlap) — the single source
    cadence         academic-calendar math: is_off_cadence / next_term / standing
    urgency         the demand urgency-scoring core, shared by all 3 sources
    solver_util     CP-SAT helpers (worker-count resolution, etc.)
    scratchdb       throwaway SQLite copies for what-if runs (never shutil a WAL db)
    xlsx_util       CSV → styled Excel worksheet
    job_lock        process-wide "only one solve at a time" guard
    graph           NetworkX prerequisite graph + bottleneck detection

**Data in** — turn files/uploads into DB rows:
    ingest          the CSV/YAML loaders (one per entity) + Banner import
    uploads         web-upload wrapper around ingest (clear-then-load atomically)
    offerings       infer Course.offer_terms / cadence from history

**Demand sources** — three ways to answer "who needs which course, how badly";
all feed the same urgency scale (see ``urgency``) and dispatch through
``generate.bootstrap_demands``:
    demand          predicted demand (semester guides + requirements)
    degreeworks     observed demand from DegreeWorks audits
    registrations   observed demand from actual registrations (back-test)

**Solving** — build and run the CP-SAT models:
    conflicts       the demand co-occurrence matrix fed INTO the solver
                    (NB: this does NOT find conflicts in a solved schedule —
                    that's validate.py)
    engine          the semester scheduling engine (SchedulingEngine): data
                    loading, section creation, solve orchestration, extraction
    scheduler_config  SchedulerConfig / SolverResult / infeasibility hints
    model_build     ModelBuildMixin — variables + hard constraints
    penalties       PenaltyMixin — soft penalties + objective
    warmstart       WarmStartMixin — warm-start hints + two-phase placement
    staffing_check  StaffingCheckMixin — pre-solve staffing feasibility
    slotcliques     time-point clique no-overlap constraints (rooms, faculty)
    sectioning      exact student re-seating (phase B): hard per-student no-overlap
    finals          the finals-exam scheduling engine (separate model)
    generate        the generation pipeline + progress-emit protocol both UIs use

**Assess a solved (or to-be-solved) schedule** — five distinct jobs:
    doctor          data health BEFORE solving (is the input even solvable?)
    validate        hard-constraint conflicts in a solved schedule (double-books)
    schedule_quality student quality-of-life (gaps, lunch, long days)
    viability       which sections are below minimum enrollment
    recommend       actionable advisories (unscheduled, over-capacity, overload)
    advisor         per-student sheets + "why is this section here?" analysis
    seatswap        pacing-aware assignment of students to section seats
    schedule_diff   what a re-solve actually changed vs the prior version

**What-if runners** — each copies the DB to a throwaway scratch file (via
``scratchdb``) and re-solves it, never touching the source:
    scenarios       resource tweaks (add a room, a section) re-solved and diffed
    stress          scale the incoming class up/down, re-solve each factor
    profiles        named weight bundles solved side-by-side (a Pareto table)
    longrange       full-solve the next N terms forward
    replan          one student's fastest remaining path to graduation

**Data out:**
    export          Banner CSV export
    ical            .ics calendar export (instructor / room / student)

Two conventions worth knowing
-----------------------------
* **Analyzers take a live ``Session`` as their first argument** (doctor,
  validate, schedule_quality, viability, recommend, advisor, seatswap, …).
* **What-if runners take a source DB *path*** and copy it — they open their own
  sessions against the scratch copy, so the caller's session is never at risk.
"""
