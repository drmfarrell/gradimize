# Handoff — 2026-07-29

## Where things stand

Four tracks:

0. **TUI-first parity (2026-07-29, owner's stated round-1 workflow).** The
   owner will work mostly in the TUI; web is second fiddle. Shipped: folder
   -based import screen (`M`; scans `$GRADIMIZE_DATA_DIR/incoming` else
   `data/incoming` for canonical filenames, imports via the same dry-run
   -protected `import_data_file`), pin/unpin (`T`), re-solve (`O`) and
   repair (`K`) — both HAND OFF to the dashboard via `app.pending_job` +
   dashboard `on_screen_resume` so they reuse its log + live viz (job lock
   acquired by the schedule screen, released by the dashboard worker),
   diff vs next-older (`Y`), conflict check (`Z`), explain on Enter.
   TUI pilot smoke tests exist now (tests/test_tui_smoke.py) — NOTE its
   fixture patches `connection.DEFAULT_DB_PATH`, NOT the env var, because
   connection.py bakes GRADIMIZE_DATA_DIR at import time; an earlier
   version of the test accidentally drove the demo DB (verified cleaned:
   schedule 1 still 151/151 pinned, no 'Pinned in TUI' rows). Still
   web-only by choice: inline section edit, projections management,
   faculty prefs editing, replan, recommendations, calendar grid,
   per-student iCal/sheet.
   FOLLOW-UPS: recommendations ([J]) and projections (browser view [5] +
   Settings [7] include_projections — a flag the TUI previously never
   passed at all) were added next. A high-effort review pass then fixed
   real bugs: in the agent-built import screen the folder Input stole
   initial focus so the letter bindings typed into the path (AUTO_FOCUS
   now targets the table; a pilot test asserts it), the post-import
   rescan wiped the results log, the worker read a widget off-thread,
   and read_bytes was unguarded; in schedule.py, #export-status was
   height:1 — clipping EVERY multi-line report including pre-existing
   viability/quality — and _row_section_ids could go stale. So
   "projections management" and "recommendations" are no longer web-only.

1. **Registrar/IT data request — unchanged and still the priority.** The
   package at `exports/data_request_package_2026-07-27/` (+ zip) is complete
   and send-ready. **Owner action, not ours.** See the "Data request" section
   below for the runbook.
2. **Live solver visualization — built, verified, shipped** (`54009c5`).
3. **FERPA-safe run logs + digital isolation — built, verified, shipped.**
   508 tests pass. Isolation ladder (owner asked "how do we digitally
   isolate — your rules are only so good", 2026-07-29): layer 1 layout/git;
   layer 2 mechanical: `.claude/hooks/guard_ferpa.py` PreToolUse hook
   (fail-closed; battery-tested standalone, 25 cases) registered in NEW
   `.claude/settings.json` (deny rules + hook; the user's
   settings.local.json untouched) + `.claude/frugal-sensitivity.json`
   (blocks student-data paths/content from ALL subagents); layer 3
   (separate unix user, chmod 700) documented in USER_GUIDE as optional
   owner step — the only true enforcement. Safe OUTPUT inspection:
   `scripts/run_report.py` (student-free grid/counts/coverage/utilization →
   logs/report-schedule-<id>.md; deliberately never imports Student tables —
   tests/test_run_report.py enforces incl. canary). serve.sh pins
   ("data never in repo, logs never in data dir" — `runlog_dir()` now
   IGNORES GRADIMIZE_DATA_DIR entirely; the first real run_report.py
   invocation proved the old DATA_DIR/logs fallback wrong by writing into
   the zone, which is also why /mnt/nvme/gradimize-data/logs may contain a
   stray synthetic-era report). GRADIMIZE_DATA_DIR **is exported in the
   owner's shell already** and the NVMe zone exists with a synthetic test
   DB (3 test schedules). NOTE: the hook may not be active until the
   session/settings reload — verify with a data/ read attempt.
   Guard limits (documented in the hook): it sees tool inputs, not program
   behavior — a script that opens the DB internally is covered by run-log
   design + layer 3, not the hook. `--show-values` is also blocked for
   Claude (it would print ids into context).
   Purpose (owner's requirement 2026-07-29): when real data arrives, Claude
   debugs by reading `logs/run-*.jsonl` ONLY — never the DB, never the CSVs —
   so FERPA-protected student data (ids even pseudonymized, grades,
   per-student attributes) never enters LLM context. Course codes, rooms,
   faculty ids, counts ARE allowed (owner drew the line at FERPA).
   Core: `src/core/runlog.py` (RunLog / describe() shapes / scrub_text /
   frame-only exceptions). Wired: uploads + ingest (`_add_error_student`),
   registrations, banner route, doctor (`protected` checks redacted in log;
   CLI redacts by default, `--show-values` restores), and a `_teed` emit
   wrapper in all three generate.py runners (solve/resolve/repair).
   Enforcement: `tests/test_runlog_canary.py` sweeps ZZ9… sentinels through
   import + doctor + tee. Log dir: `$GRADIMIZE_LOG_DIR` →
   `$GRADIMIZE_DATA_DIR/logs` → repo `logs/` (gitignored); newest 60 kept.

**Still no real institutional data has ever been received.** Everything runs
on synthetic data. The pipeline remains untested against a real Banner extract.

## Next step

Data request: owner sends the package. When extracts land, the runbook is
unchanged: `python scripts/doctor.py` → fix every FAIL → transcribe the
meeting-time grid into `config/time_slots.yaml` and requirement sheets into
`degree_requirements.csv` → import (catalog → prerequisites → rooms → faculty
→ students → history → registrations → historical schedules) →
`scripts/derive_teaching_history.py` → run the two experiments (see
`memory/project_fall2026_backtest.md`).

Visualization: nothing outstanding. It is complete and committed. Optional
polish only, listed under "Known rough edges".

## In flight

Nothing. Working tree is clean, all four commits pushed.

**`stash@{0}` still exists and should be dropped once the owner has eyeballed
`exports/gradimize_concept_paper.py`.** It is a leftover backup from the
incident below, not work in progress. It duplicates content already committed
in `62b374b`.

## The git stash incident — read before spawning subagents

A subagent ran `git stash` mid-session. That discarded **all** uncommitted
tracked-file changes, including the owner's unrelated 64-line edit to
`exports/gradimize_concept_paper.py`. It was recovered via
`git checkout stash@{0} -- <path>` and committed (`62b374b`); nothing was
lost. Note `git stash push` logs a reflog entry reading
`reset: moving to HEAD`, which looks exactly like a destructive `git reset`.

**Every subagent prompt must forbid git commands that mutate the working tree
or index** (`stash`, `reset`, `checkout`, `commit`, staging). Read-only
`git status`/`git diff` is fine. After adding that constraint it did not
recur across six further agent runs.

## Verified vs assumed

- **Verified:** `504 passed` — `.venv/bin/python -m pytest tests/ -q` (~46s).
  42 of those are run-log tests, incl. the canary sweep (sentinels planted in
  student CSVs → import/doctor/tee → grep every log file) and a
  threads-writing-concurrently JSONL integrity test.
- **Run-log design decisions a future session must not "fix":**
  - `scrub_text` blanks only *space-free* quoted tokens. Deliberate: the
    obvious broader regex eats the span between apostrophes in prose
    ("doesn't … can't"). Spaced values are kept out structurally (student
    paths drop exception messages entirely, `include_msg=False`), not by
    scrubbing. Don't widen the regex.
  - `describe()` withholds punctuation characters for values ≤4 chars —
    listing them would nearly reconstruct short enums (grade "A+").
  - Doctor `summary` strings flow verbatim into the run log; that is why the
    staffing-preflight failure summary carries only the exception TYPE.
    Any new check whose details hold student data must set `protected=True`.
  - stdlib `logging` is deliberately NOT piped into run logs — existing
    console messages embed row values and must stay console-only.
  - `/data/health` and TUI `[h]` are on-demand (verified: nothing polls
    them), so per-call doctor log files cannot churn the 60-file retention.
- **Not yet runlog-wired (deliberate scope cut):** headless scripts
  (`retrospective_sim.py`, `absorption_test.py`, `stress_test.py`) and the
  synchronous finals solve. Wire them when they're next touched.
- **Verified against a real CP-SAT solve** (not stubs), through the web UI via
  Playwright on a scratch DB copy: phases progressed Setting up (72 log lines,
  0 solutions) → Improving (294 lines, 8 solutions) → Certifying (307 lines,
  21 solutions, gap 16.2%), finishing **feasible, 156 sections, 4340
  enrollments, 325s**. `tests/test_solver_viz_integration.py` asserts the same
  path headlessly, and asserts `--no-viz` yields exactly zero viz events.
- **Measured on the synthetic 1500-student set** (NOT the real ~700-student
  instance, so treat as an upper bound): 39s model build, then **47s of CP-SAT
  presolve** before search starts, first incumbent ~90s in. This is why the
  "searching" phase and the grid placeholder exist.
- **Assumed / best-effort:** CP-SAT log parsing (bound, restarts, conflicts,
  the "Starting search at Xs with N workers" phase trigger). The log's wording
  is **not an API contract**. Every parsed field is optional; an OR-Tools
  upgrade could silently stop populating specific counters and the display
  would degrade rather than break (test:
  `test_unparseable_log_lines_are_harmless`).
- **Assumed (unchanged from before):** Banner + DegreeWorks field names were
  checked against *public* Ellucian ERD/ODS references, **not** the institution's
  Banner instance.

## Rejected approaches — do not re-litigate

Visualization:
- **An LNS "attempts tried / kept" counter.** Pitched during design, then
  dropped: CP-SAT does not expose accepted/rejected neighborhood counts
  without depending on log wording that is not an API contract. The footer
  shows only reliable values (solutions, moved, conflicts, branches,
  restarts, elapsed).
- **Unmet-demand heat as the grid coloring.** Would require re-running demand
  satisfaction on every incumbent. Department coloring is free (the course
  code carries the subject) and more legible. Owner chose it.
- **Pure abstraction (particles / constraint-graph pulsing).** Rejected in
  design: partly fabricated motion, tells a Registrar audience nothing.
- **Any timer- or CSS-driven spinner.** The spinner advances off an
  event-carried `tick` so motion stops when data stops. A spinner that keeps
  turning while the backend is dead is a lie, and this UI's whole purpose is
  trust during a silent ten-minute run.
- **A second animated element on the grid placeholder.** One liveness
  indicator informs; two competing ones are noise.

Data request (unchanged, still binding):
- Demand from a pre-registration DegreeWorks audit ("Design B") — rejected
  June 2026, burdens the Registrar.
- "Validate a small sample before the full pull" — removed 2026-07-28; at
  ~700 students a sample is identical IT effort and doubles round-trips.
- Codifying the handling document as University policy — policy office said
  no new policy is required; owner declined codification.
- Shipping `reidentify.py` in the first package — deliberately held for
  phase 2 (exists in git at `adc870a`).
- Adding a graduate class standing — institution is undergrad-only.
- CI — offered and declined by the owner (2026-07-18). Run tests manually.

## Known rough edges (visualization) — cosmetic, not bugs

- The footer can read `conflicts 0` while `branches` is large. That is CP-SAT
  genuinely reporting zero conflicts for the main worker while LNS workers do
  the work. Real, if odd-looking.
- The convergence chart's objective series sits flat at the bottom while the
  bound drops from far above; correct but visually sparse early on.
- `SolverViz` is wired into `GenerateSchedule` and `ReSolvePanel`. The finals
  solver (`POST /api/finals/{id}/solve`) is synchronous, not SSE, so it has no
  viz. Adding it would mean converting that endpoint to the streaming shape.

## Traps

- `exports/archive/**` is a **frozen snapshot** of the 2026-07-20 package and
  deliberately keeps the old `DATA_REQUEST_POLICY` filename. Never "fix" its
  stale references.
- The frugal plugin **blocks generic agent types** (`general-purpose`,
  `Explore`, `Plan`, `claude`). Use `frugal:scout` / `frugal:extractor` /
  `frugal:mechanic` / `frugal:builder` / `frugal:sage`. Inline search is also
  budget-capped at 5 ops per prompt; the budget resets when you delegate.
- `Claude.md` says "~1,500 students"; the owner states **~700**. Owner declined
  to correct it ("we'll deal with the real data when we get it"). The demo DB
  genuinely has 1500 synthetic students, which is why measurements above cite
  that number.
- Any test or script that runs a solve must set `GRADIMIZE_DATA_DIR` to a
  scratch dir. The Playwright run used a copy at
  `<scratchpad>/vizdemo/gradimize.db`; `data/gradimize.db` was verified
  untouched afterwards (mtime unchanged).
- Playwright: the venv's `playwright` package needs browser build **1228**
  (`.venv/bin/playwright install chromium`); the pre-existing cache only had
  1217. The solve form is behind a **"Generate Schedule" disclosure button** on
  the Dashboard — `#gen-time` does not exist until it is clicked.
- Regenerating the data-request package after editing any source markdown:
  ```
  .venv/bin/python exports/gradimize_data_request_docx.py            # the request
  .venv/bin/python exports/gradimize_data_request_docx.py SRC.md OUT.docx
  # copy sources into exports/data_request_package_2026-07-27/, then:
  cd exports && rm Gradimize_Data_Request_Package_2026-07-27.zip \
    && zip -rq Gradimize_Data_Request_Package_2026-07-27.zip data_request_package_2026-07-27
  ```

## Open decisions for the owner

- Drop `stash@{0}` once the concept paper is confirmed good.
- Cover email (`exports/email_draft_data_request.txt`) still has `[Name]` as
  the signature and a bracketed line about the signed agreement — keep or cut.
- Whether to ever reintroduce the re-identification material (phase 2).
