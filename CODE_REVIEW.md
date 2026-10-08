# Gradimize Code Quality Review — 2026-07-14

Full-codebase review (solver engine, core pipeline, API, database, TUI, scripts, tests,
React frontend) done before the next round of feature work. Findings are ordered by
priority; each is a checkbox so this doubles as a punch list.

**Baseline:** all 82 tests pass (on the rebuilt Python 3.12 venv — see Environment at the
bottom). `tsc -b` passes with strict mode in `web/`. Ruff (project's E/F/I/W rules) reports
210 issues, mostly mechanical (119 line-length, 45 unused imports, 38 unsorted imports).

---

## P0 — Critical: fix before anything else

### 1. Replace-import destroys data on a bad upload
`src/core/uploads.py:171-176` — when `replace=True` (the default for the web Import screen),
all existing rows are deleted **and committed** before the uploaded file is parsed. If the
file is malformed (bad header, non-numeric capacity cell, …), the `except` at line 184 rolls
back only the failed load — the wipe is already permanent. Result: upload a typo'd
`courses.csv` → entire course catalog gone, HTTP 200 with `rows_loaded=0`, schedules now
reference orphaned courses (SQLite FKs are not enforced).

- [x] **Done 2026-07-14.** Deletes now run uncommitted (bulk `sa_delete`) in the same
      transaction as the loader; the loader's final commit lands wipe+reload atomically and
      a parse failure rolls the wipe back. Regression-tested in `tests/test_uploads.py`.
- [x] **Done 2026-07-14.** `load_rooms` equipment column fixed (same pattern as
      `load_courses`); regression test added.
- [x] **Done 2026-07-14.** `is_active` now parses string booleans like `load_students`
      does; regression test added.

### 2. The solver config is largely fiction
Three compounding problems:

**(a) `config/constraints.yaml` is never loaded.** Every production entry point constructs
`SchedulerConfig()` with no path: `src/core/generate.py:100,177` (backs the web
generate/re-solve), `src/api/routes/schedule.py:69,616`, `src/tui/screens/dashboard.py:220`,
`scripts/benchmark_solver.py:108`. Only `run_solver.py --config` can load it. Concretely:
the "Faculty Common Hour" blocked period (MWF 12:30-14:30, `constraints.yaml:66-71`) is
silently ignored — every schedule generated from the web UI or TUI can place classes inside
the common hour.

**(b) Six of the nine hard-constraint flags are decorative.** `room_no_overlap`,
`faculty_no_overlap`, `respect_room_capacity`, `faculty_approval_required`,
`linked_no_overlap`, `student_no_overlap` are defined in `SchedulerConfig` and the YAML but
never read anywhere in `src/`. Only `match_room_type`, `min_travel_time`,
`graduation_critical_required`, and `faculty_load_limit` are consulted. (This explains the
March debugging memory "INFEASIBLE even with all hard constraints disabled" — the toggles
never did anything.)

**(c) Room capacity is never enforced at all.** Despite `respect_room_capacity: true` and
the comment at `engine.py:318` claiming "(capacity still enforced)", nothing compares
`section.capacity` (hardcoded 30 at `engine.py:448`) with `room.capacity`. Compatible rooms
are filtered by type only (`engine.py:345-352`), so a 30-seat section can be placed in a
12-seat seminar room.

- [x] **Done 2026-07-14.** `SchedulerConfig.load_default()` loads `config/constraints.yaml`
      when present; all six production entry points use it (generate.py ×2, API routes ×2,
      TUI dashboard, benchmark; run_solver.py falls back to it without `--config`). Bare
      `SchedulerConfig()` stays hermetic for tests. Smoke-verified: Faculty Common Hour now
      blocks 4 slots and no meeting lands in them.
- [x] **Done 2026-07-14.** Room capacity enforced in `_add_capacity_constraints` as
      `enrollment <= Σ room.capacity·X` per meeting — better than filtering rooms out:
      small rooms stay usable for low-enrollment sections, and multi-meeting sections with
      small labs keep both meetings (a room filter broke exactly that in tests).
      Regression tests added in `tests/test_solver.py`; smoke-verified on sample data.
- [x] **Done 2026-07-14.** `room_no_overlap`, `faculty_no_overlap`, `linked_no_overlap`,
      `faculty_approval_required` are now honored (early-return / all-approved bypass);
      `student_no_overlap` deleted from config + YAML with a comment documenting the
      soft-penalty design, and the dead `_add_student_overlap_constraints` removed.
      USER_GUIDE.md and Claude.md updated to match.
- [x] **Done 2026-07-14.** `back_to_back_penalty.student` and `max_consecutive` removed
      from defaults, YAML, and docs; the faculty penalty is documented as per-adjacent-pair.

### 3. Public-tunnel deployment exposes an unauthenticated, destructive API
The cloudflared tunnel (serve.sh) exposes the Vite dev server whose `/api` proxy forwards
every FastAPI route to anyone with the URL: `DELETE /schedule/{id}`, `DELETE /finals/{id}`,
`POST /data/import/{kind}` (wipes tables, `replace` defaults true — see P0.1),
`PATCH` on sections/faculty, and `GET /schedule/generate/stream` (starts a CPU-heavy solve;
being a GET with side effects it can be triggered by any web page's `<img>` tag).

- [x] **Done 2026-07-14 (revised per owner): public tunnel access DELETED instead of
      adding auth.** serve.sh no longer starts cloudflared (localhost/LAN only);
      `--no-tunnel` flag and tunnel log/URL plumbing removed; `vite.config.ts` dropped
      `allowedHosts: true` and the HTTPS-tunnel HMR override. Re-add auth before ever
      exposing this publicly again.
- [x] **Done 2026-07-14.** CORS now enumerates the two localhost dev origins with
      `allow_credentials=False` (browser traffic goes through the same-origin Vite proxy
      anyway).
- [x] **Done 2026-07-14.** Both upload paths capped at 50 MB → 413 (`/data/import/{kind}`
      reads at most cap+1 bytes; `/schedule/import` checks `csv_content` length).
- [x] **Done 2026-07-14.** All five leaky handlers now `logger.exception(...)` server-side
      and return a generic "Internal server error" detail.
- [x] **Done 2026-07-14.** `/data/import/{kind}`, `/projections/generate-demand`, and
      `DELETE /projections/generated-data` return 409 while an optimization holds
      `job_lock`. *(2026-07-18: the two projections routes were later deleted in the
      P3 dead-code pass; the then-orphaned `_reject_if_optimizing` helper and stale
      route-ordering comment were removed once test coverage caught them.)*
- [x] **Resolved 2026-09-22 — nothing further to prune.** The P3 pass (2026-07-18)
      already removed the 18 genuinely dead routes named here (57→39). A fresh audit of
      all 48 current routes against `web/src` + `tests/` found: 24 frontend-used,
      17 test-covered feature surface (iCal/banner exports, advisor explain-move,
      rebalance, replan, faculty preferences — all roadmap or deliberately kept),
      `POST /schedule/import` (Fall'26 back-test), and 3 unreferenced-but-kept:
      `GET /api` + `GET /api/health` (ops probes; health is referenced by the VM
      deployment) and `GET /recommendations/{id}` (advertised feature, TUI-adjacent).

---

## P1 — User-visible correctness bugs

- [x] **Done 2026-07-14.** Both schedule export endpoints now return `text/csv` downloads
      (shared `_csv_download` helper, same pattern as finals); `ExportResponse` schema
      deleted. Covered by new `tests/test_api_schedule.py`.
- [x] **Done 2026-07-14.** Partial pin bodies default the missing slot/room from meeting 0
      and 422 when there is nothing to default from; drag-moves can no longer
      silently pin without moving. Frontend surfaces the error and reverts. Tested.
- [x] **Done 2026-07-14.** `snapSlot` now makes day membership dominant (other-day slots
      only when the type doesn't meet on the hovered day), and the preview renders in
      every day column of the snapped slot.
- [x] **Done 2026-07-14.** `useSolverStream.start()` pre-flights the optimization status
      (real message for the 409 case) and on mid-solve connection loss falls back to
      polling until the run ends, then navigates to the newly created schedule.
- [x] **Done 2026-07-14.** saveEdit/moveSection/runConflictCheck/togglePin all surface
      backend errors via a dismissible banner; failed moves reload server state.
      The dirty warning also clears after a successful conflict check.
- [x] **Done 2026-07-14.** Selection and solve message reset on schedule change via a
      `useEffect` keyed on `selectedScheduleId`.
- [x] **Done 2026-07-14.** delete_schedule now removes finals schedules/blocks/assignments
      and uses bulk deletes for meetings/enrollments/sections (sync def). Tested.
- [x] **Done 2026-07-14.** Unknown faculty ids are skipped with a warning instead of a
      KeyError.
- [x] **Done 2026-07-14.** Meetingless sections emit a row with blank meeting columns.
      Also fixed here: course codes split at the first digit instead of `[:4]`
      (3-letter subjects were corrupted).
- [x] **Done 2026-07-14 (pulled forward from P1 — hit live during the P0 smoke test).**
      `build_matrix` now assigns `self.matrix = pd.DataFrame()` on the no-demands path;
      all three accessors handle the empty frame gracefully.
- [x] **Done 2026-07-14.** The single best boost applies once per course (no compounding);
      one reason string. Guide tests pass unchanged.
- [x] **Done 2026-07-14.** Passing set derives from `grade_order` for every threshold
      (default "D" no longer counts "D-" as passing).
- [x] **Done 2026-07-14.** Rows sort by chronological day index (handles both block
      labels and in-place multi-day strings).
- [x] **Done 2026-07-14.** Schedule/DataBrowser/Finals screens reload in
      `on_screen_resume`, with index clamping when lists shrink while suspended.
- [x] **Done 2026-07-14.** TUI generate and finals solves acquire the same `job_lock` as
      the web API (released in finally), so TUI+web share one optimization slot.
- [x] **Done 2026-07-14.** `load_prerequisites` validates both codes against the catalog
      and reports unknown codes as import errors instead of creating phantom rows.
- [x] **Done 2026-07-14.** A pinned placement that no longer exists logs a warning and the
      meeting is re-assigned normally instead of silently vanishing.

---

## P2 — Scale readiness (matters for the 5,000-student real-data rollout)

### 1. One engine, correct SQLite settings, indexes
- [x] **Done 2026-07-14.** Engines are cached per DB path (one pool per process);
      `connection.get_session` is the single FastAPI dependency — the seven per-module
      copies are gone. Test suite got ~2x faster from this alone.
- [x] **Done 2026-07-14.** WAL + busy_timeout=15s + synchronous=NORMAL set via a connect
      hook. `PRAGMA foreign_keys` deliberately stays OFF: replace-import clears parent
      tables while old schedules reference them, and cascades are manual — enforcement
      needs an import redesign first (documented in connection.py).
- [x] **Done 2026-07-14.** `index=True` on all 11 hot FK/filter columns + matching
      `CREATE INDEX IF NOT EXISTS` migrations for existing DBs. Unique constraints on
      enrollments/meetings deferred (needs a dupe check on live data first).

### 2. Load-all-to-count and N+1 patterns (16 sites)
- [x] **Done 2026-07-14.** All load-all-to-count sites now use `func.count()` /
      grouped counts: dashboard stats, schedule list + detail (bulk meetings +
      enrollment counts), finals list, TUI schedule screen, banner export.
- [x] **Done 2026-07-14.** recommend.py prefetches meeting-0 slots once per schedule.

### 3. Event-loop blocking in async endpoints
- [x] **Done 2026-07-14.** check_conflicts, delete_schedule, list_schedules, stats, and
      finals list are plain `def` (threadpool); their heavy work is also much lighter
      after the count-query fixes.

### 4. Hot-path algorithms
- [x] **Done 2026-07-14.** Conflict matrix accumulates pair weights in a dict and builds
      the DataFrame once. (TUI constraints screen still builds it on the UI thread —
      much faster now; moving it to a worker is tracked under P3 polish.)
- [x] **Done 2026-07-14.** Demand clearing uses bulk DELETEs in demand.py (both sites,
      rowcounts preserved for stats) and degreeworks.py.
- [x] **Done 2026-07-14.** Requirement-category maps and guide entries cached per
      (major, concentration, catalog_year) like the other requirement lookups.
- [x] **Done 2026-07-14.** Travel-time constraints index assignments by slot and only
      pair tight-adjacent slot pairs.
- [x] **Done 2026-07-14.** `_extract_solution` does one pass over the X dict; redundant
      per-section scans and the linear CRN lookup in enrollment extraction removed.
      Smoke-verified: identical model size and a feasible solve after all P2 changes.

---

## P3 — Maintainability: dead code, duplication, consistency

**Status 2026-07-14: pass complete.** What was done, and the deliberate leftovers:

### Done
- [x] Dead code purge (~1,900 lines): queries.py 465→104, `src/tui/widgets/` package,
      graph.py dead methods, uncalled module wrappers, 18 dead API endpoints + guides
      router + 8 orphan schemas (57→39 routes), stray dead locals/attributes.
      Kept deliberately: `POST /schedule/import` (Fall'26 back-test workflow) and
      faculty detail/preferences endpoints (tested feature).
- [x] `src/core/timeutil.py`: one home for time_to_minutes / times_overlap /
      slots_overlap / parse_semester (was 3-4 diverging copies; validate.py and
      offerings.py no longer import engine underscore-privates).
- [x] `src/core/urgency.py`: shared scoring core for both demand sources (base
      standing score, bottleneck ×2, senior ×1.5, required ×1.5, grad-critical rule).
- [x] `src/core/xlsx_util.py`: shared CSV→worksheet styling (was duplicated verbatim).
- [x] TUI generation pipeline now calls `core.generate.run_generation` — the diverged
      copy (which counted stale projected demands) is gone; the demand-source-aware
      bootstrap (`bootstrap_demands`) moved into generate.py and is used by the web
      flow (which previously ignored `demand_source: degreeworks`), the TUI, and
      run_solver.py.
- [x] Solver status vocabulary normalized to lowercase everywhere (finals engine
      emitted uppercase; consumers hardcoded matching case).
- [x] `create_sections` magic numbers → `SchedulerConfig.section_settings`
      (default_capacity / students_per_section / max_sections_per_course / crn_start),
      YAML-overridable.
- [x] Misleading `stats["total_urgency_served"]` removed (objective ≠ urgency served).
- [x] Semester no longer hardcoded: TUI Settings has an editable semester (plus
      `--semester` flags on the TUI and run_solver.py; benchmark parameterized);
      Settings also fixed so workers can be set back to 0 (auto) with validation
      feedback instead of silent discards.
- [x] uploads.ImportResult renamed FileImportResult (two same-named classes in one
      package); projections delete response matches the `{"message": ...}` convention;
      finals module docstring no longer claims soft penalties are hard constraints;
      models.py in-place threshold comment matches the 18:00 default; TUI exports
      anchor to the repo root instead of CWD; generate.py markup-stripper no longer
      eats `[WARN]`-style text.
- [x] Frontend: shared `format.ts` (12-hour time — two copies had diverged "9a"/"9am" —
      newest-first sort ×6, timestamp format), typed FinalsSolverStats, dead exports
      removed.
- [x] retrospective_sim.py: conflict metric implemented (the `slot_overlaps` parameter
      was dead and the docstring promised a metric that didn't exist); grad-critical
      comparison now reports "offered" on BOTH sides (apples-to-apples) with Gradimize's
      enrolled count separate.
- [x] Ruff: unused imports and import sorting clean across src/tests/scripts.

### Deliberately deferred (revisit when touching the area)
- [x] **Done 2026-09-22.** `created_at` → real datetime columns (construction sites
      updated, API still serializes isoformat, ordering semantics tested against
      legacy string rows in tests/test_database_migrations.py).
- [x] **Done 2026-09-22.** `Section.needs_final` bool (NULL→1 backfill matches
      finals.py semantics), `FreshmenProjection.semester` sentinel cleaned,
      `Course.linked_course_code` FK declared (declarative only).
- [x] **Done 2026-09-22.** Unique indexes on enrollments (section_id, student_id)
      and meetings (section_id, meeting_sequence) via a guarded migration that
      counts duplicates and warns-and-skips instead of crashing (no real data
      exists yet — owner confirmed 2026-09-22 — so the guard is precautionary
      for any DB delivered later).
- [ ] PRAGMA foreign_keys=ON — **still deferred, decision reaffirmed 2026-09-22:**
      requires cascade rules + import redesign (documented in connection.py);
      too risky to change with the real-data delivery imminent.
- [x] **Done 2026-09-22.** E501 backlog cleared (ruff fully green); frontend
      reset-state refactors done as part of clearing ALL react-hooks findings
      (0 errors / 0 warnings, including two errors newer lint rules surfaced
      in SolverViz). Still open, genuinely minor: a useMutation-style hook for
      the remaining `{ok,text}` banners; TUI conflicts screen builds the (now
      fast) matrix on the UI thread.

## Tests — status

- [x] The two no-op solver tests now assert the solve status first (an INFEASIBLE
      regression fails loudly).
- [x] `tests/conftest.py` provides the shared `db_manager` fixture (module-local
      copies still override until removed module-by-module).
- [x] Finals engine: first coverage — solve/assignment/in-place/shared-student
      separation + CSV export (`tests/test_finals_engine.py`).
- [x] Student-overlap semantics pinned: overlap is a soft penalty by design, and
      `check_schedule_conflicts` is what surfaces residuals — tested in
      `tests/test_validate.py` (validator's first coverage).
- [x] API layer: `tests/test_api_schedule.py` covers CSV exports, the pin contract,
      and the delete cascade; `tests/test_uploads.py` covers the import transaction.
- [x] Frontend: ESLint (flat config, react-hooks) wired in; `npm run lint` has
      0 errors / 7 tracked warnings.
- [x] **Done 2026-07-18 (commit 25b1033; checkbox updated 2026-09-22).** All five
      gaps covered: tests/test_degreeworks.py, test_offerings.py, test_generate.py
      (emit protocol + markup stripping), test_api_schedule_stream.py (SSE + 409),
      test_api_job_lock.py.

## Environment notes (already handled today)

- The system Python was upgraded 3.11 → 3.12, which had broken `.venv` entirely. Rebuilt at
  `.venv` on Python 3.12 with the same pinned package versions; **all 82 tests pass**.
  *(2026-09-22: `.venv-py311-broken` deleted.)*
- `pip`/`venv` ARE available on system Python 3.12 (old "no pip" note is stale).
- *(2026-09-22)* `poetry.lock` dropped in favor of `requirements-lock.txt` (pip freeze of
  the known-good 3.12 venv; `scripts/bootstrap.sh --locked` consumes it).

## Suggested order of attack

1. **P0.1 + P0.2** (data-loss import; config loading + room capacity) — small diffs, huge
   correctness payoff, all covered by existing test infrastructure.
2. **P0.3 minimum auth token + upload caps** before the next public demo.
3. **P1 frontend/export fixes** (broken CSV links, pin no-op, error surfacing) — these are
   what beta users will actually hit.
4. **P2.1 engine singleton + pragmas + indexes** — prerequisite for the real-data rollout.
5. The P3 dead-code deletion pass (queries.py, widgets/, graph methods, wrappers) — ~1,500
   lines removed, makes everything after it easier.
6. Test gaps (finals engine, student overlap semantics, conftest consolidation) as the new
   features' foundation.
