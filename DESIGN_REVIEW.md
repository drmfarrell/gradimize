# Gradimize Design & Navigability Review — 2026-07-21

Scope: structure and readability for a human reader — monolith risk, module boundaries,
naming, signposting, duplication. Correctness/perf/dead-code were covered by the
2026-07-14 pass (CODE_REVIEW.md); its deliberate deferrals are not re-reported here.
Method: four parallel area reviews (solver engine, core package, API+database, TUI+web)
plus an import-graph analysis. Every behavior-relevant claim below was re-verified by
hand before inclusion.

---

## Progress — 2026-07-21

**Done and verified (326 tests pass, up from 323; ruff clean on touched files):**

- **All five P0 bugs fixed**, each with a regression test where backend-testable:
  the stale `_open_vars` diagnosis guard (+ test), the stale `_times_overlap` test
  import, the `/data/import` lock race (now holds the slot; + test), the
  SectionDialog panel-closes-on-click bug, and the per-guide-commit atomicity break
  (+ single-commit test).
- **P5 signposting complete**: package-map docstrings in all five `__init__.py`
  (core's is the module-by-module map), a README repository-layout section, the
  rewritten Claude.md architecture diagram + structure section, and disambiguating
  docstrings on the misleading names (`conflicts` vs `validate`, `export`, `demand`).
- **P1 — engine split COMPLETE.** The 3,082-line `engine.py` is now six files, the
  largest 994 lines:
  - `scheduler_config.py` (300) — `SchedulerConfig` / `SolverResult` / hints.
  - `model_build.py` (803) — `ModelBuildMixin`: variables + hard constraints.
  - `penalties.py` (622) — `PenaltyMixin`: soft penalties + objective.
  - `warmstart.py` (324) — `WarmStartMixin`: hints + two-phase placement.
  - `staffing_check.py` (207) — `StaffingCheckMixin`: pre-solve staffing.
  - `engine.py` (994) — data loading, section creation, solve orchestration,
    infeasibility diagnosis, extraction.

  **Design note:** I used **mixins** rather than the review's proposed `ScheduleModel`
  composition. Deliberate, given the flagged silent-CP-SAT-regression risk: the method
  bodies moved **byte-for-byte identical** (an AST-precise extraction), so the only
  changes are relocation + composing four base classes — no state re-threading, no
  chance of a mis-wired constraint. Each mixin's docstring documents the `self`
  contract. Also hardened the `_open_vars` pattern (initialized in `__init__`; the 8
  fragile `getattr` sites — the root of the P0 diagnosis bug — are gone).

  **Verified three ways:** 326 unit tests pass; the full-scale model builds identically
  (121,863 vars / 80,590 constraints); and an end-to-end solve on the dev dataset
  produced a feasible 156-section / 4,360-enrollment schedule (matching the historical
  baseline). The `ScheduleModel` composition remains an option later if preferred over
  mixins, but it is no longer needed to de-monolith the file.

- **P2 — API route split COMPLETE (plus the worst layering leaks + job_lock
  consolidation).** `routes/schedule.py` (996 lines) is now a `routes/schedule/`
  package: `crud.py` (238), `solve.py` (106, SSE streams), `pins.py` (196),
  `analysis.py` (283), `exports.py` (125), and an `__init__.py` that combines the
  sub-routers and re-exports `get_session` — so `from src.api.routes.schedule import
  router, get_session` is unchanged and all 20 paths mount identically. Also:
  `explain_section`'s ~110 lines of domain logic moved to `core/advisor.py` (route is
  now a 3-line delegate, and the route→route import is gone); the conflict-matrix
  cache moved from `routes/conflicts.py` to `core/conflicts.py` (now shareable with
  the TUI); `job_lock` gained a canonical `BUSY_MESSAGE` + a `hold()` context manager,
  and the four generic 409 sites (generate/re-solve/repair/rebalance) plus `finals.py`
  now use the one message; fixed the `data.py` import-wedge (E402). Verified: 326 tests
  pass; the app imports and mounts under `/api`.

- **P4 — frontend monoliths split.** `ScheduleView.tsx` 876 → 420 (the 6 sibling
  components + 2 inline cards extracted into a new `screens/schedule/` package);
  `CalendarView.tsx` 707 → 467 (pure layout logic → `calendarLayout.ts`, block
  renderer → `EventBlock.tsx`). Verified: `tsc -b` clean, ESLint unchanged (0 errors,
  same 10 pre-existing warnings — no new ones), and `npm run build` bundles all 62
  modules. Deferred with reason: the stateful-hook extractions (`usePinning`,
  `useSectionMutations`, `useCalendarDrag`) and the folder reorg — a behavioral
  regression there would be invisible with zero frontend tests, so the recommended
  next step is to add vitest + tests for the now-pure `calendarLayout.ts` first.

**Remaining (each independently reviewable):** the frontend hook extractions + first
vitest tests, the smaller P2 polish (pin-apply logic → core, shared delete-cascade
helper, `schemas.py` colocation/header cleanup), the intra-engine dedups
(`effective_capacity()` ×4, `re_solve`/`repair_solve` clone block), the TUI `base.py`
shared-boilerplate module, and the P3 shared-concept promotions. Checkboxes below
reflect this state.

**Verdict up front: the architecture is fundamentally healthy.** `src/` is a clean
layered DAG with zero import cycles; business logic lives in `src/core`; both UIs are
thin clients over the same core functions and the same job lock. There is exactly one
true monolith (`src/core/engine.py`, 3,082 lines) and one oversized router
(`src/api/routes/schedule.py`, 996 lines / 15 sibling imports). The two systemic gaps
are (1) **signposting** — the architecture exists but is written down nowhere a
newcomer would find it — and (2) a recurring pattern of **shared concepts trapped as
another module's underscore-privates**, forcing six cross-module private imports.

---

## P0 — Bugs surfaced by the design review (fix regardless of refactoring)

All five verified by hand.

- [x] **Done 2026-07-21.** **Stale `_open_vars` poisons infeasibility diagnosis.** `solve()` sets
      `self._open_vars` to BoolVars of the current model (`engine.py:1108`) and never
      clears it. On INFEASIBLE it calls `_diagnose_infeasibility` (`engine.py:1304`),
      which builds a **new** model and re-runs the constraint builders — and those
      builders fetch the stale vars via `getattr` (`engine.py:1668, 1821, 2317`) and
      add constraints referencing variables from the dead model. With
      `allow_section_closure` on, diagnosis either mis-constrains or dies into the
      blanket `except` at `engine.py:2877` and reports "inconclusive".
      `_solve_placement_phase` knows about this hazard and save/restores
      (`engine.py:938-957`); `_diagnose_infeasibility` does not. The P1 engine split
      (explicit build context) fixes the *class* of bug; the minimal fix is a
      save/None/restore around diagnosis.
- [x] **Done 2026-07-21.** **Stale test import fails only on a conditional path.**
      `tests/test_multi_meeting.py:266` does `from src.core.engine import
      _times_overlap`, which no longer exists (now `timeutil.times_overlap`). It sits
      inside a branch that runs only when two meetings share a day, so the suite is
      green until that day. Evidence that import-site rot doesn't surface in a green
      run — fix before any engine split.
- [x] **Done 2026-07-21.** **`/data/import/{kind}` lock check races the solver.** `data.py:155-160` checks
      `job_lock.status()["running"]` and proceeds **without holding the lock** — a
      solve that starts after the check races the import it was meant to exclude. The
      other five sites use acquire/try-finally. Fold into the `job_lock.hold()`
      context manager proposed in P3.
- [x] **Done 2026-07-21.** **SectionDialog "Why is it placed here?" panel closes the dialog on click.**
      The panel (`SectionDialog.tsx:251-263`) renders inside `.modal-overlay`
      (`onClick={onClose}`, line 117) but *after* the `.modal` card that stops
      propagation (line 118). Clicking anywhere in the explanation dismisses the
      modal. Move it into the card body.
- [x] **Done 2026-07-21.** **`load_semester_guides` commits per guide inside its loop**
      (`ingest.py:870, 884`), unlike every other loader's single final commit. This
      quietly breaks `uploads.py`'s "clear-then-load in ONE transaction" guarantee
      (`uploads.py:231-235`) for that kind: a parse failure on guide N leaves the
      wipe plus guides 1..N-1 committed. Hoist to one commit like the siblings.

---

## P1 — The monolith: split `src/core/engine.py` (3,082 lines)

The file is five coherent subsystems interleaved in an order that hides all of them.
The constraint builders already take `(model, sections, X, Y, Z, section_in_slot)`
explicitly — that discipline is what makes a split cheap. The blocker is that each
builder also reads ~6-10 `self.*` data indexes plus three hidden mutable knobs
(`_open_vars`, `_pinned_meetings`, `_repair_anchor`); the honest split is a
**model-builder class owning the per-solve state**, not free functions.

**The strongest argument is not file size:** the constraint-build pipeline is written
out three times and hand-synced — `solve()` (`engine.py:1114-1126`),
`_solve_placement_phase` (`engine.py:946-955`), `_diagnose_infeasibility`
(`engine.py:2839-2857`). A `ScheduleModel.build(include_enrollment=…, family_cb=…)`
collapses all three and structurally fixes the P0 `_open_vars` bug.

Proposed plan — keep `engine.py` as the orchestration home and re-export moved names
so the ~7 src importers and ~20 test files are untouched:

| New module | Moves (current lines) | Coupling cost | ~Lines |
|---|---|---|---|
| `scheduler_config.py` | `MeetingInfo` (35), `SchedulerConfig` (43-224), `SolverResult` (226-244), `_is_off_cadence` (246), `compute_blocked_slot_ids` (257), `INFEASIBILITY_HINTS` (321) | **Zero** — touches no `self.*`; the no-risk first commit | ~350 |
| `model_build.py` | New `ScheduleModel` class owning `model, X, Y, Z, section_in_slot, open_vars, pins, anchor`; all hard-constraint builders + `_create_variables`, `_compute_section_in_slot`, `_AssumptionProxy` | Constructor takes the engine (1 param); method signatures *shrink* | ~850 |
| `penalties.py` | All soft-term builders + `_set_objective` (1897-2007, 2188-2268, 2520-2766, 1517-1577, 2882-2981) | 1 param (`ScheduleModel`) | ~600 |
| `warmstart.py` | `_add_placement_hint`, `_solve_placement_phase`, `_hint_from_placement`, `_hint_from_anchor` (791-1047, 1579-1617); the save/restore hack at 938-957 disappears | functions over `ScheduleModel` | ~330 |
| `staffing_check.py` | `_staffing_preflight` (617-774) + finalize + `_effective_load_limit`; legitimizes `doctor.py:463`'s current private reach-in | 1 param | ~200 |
| `engine.py` residual | data loading, `create_sections`, `solve`/`re_solve`/`repair_solve`, diagnosis, `_extract_solution`, re-exports | — | ~700-750 |

Minimum-churn floor if the full plan is too much now: extract `scheduler_config.py`
and reorder the file with banner comments (data → sections → preflight → build →
solve → variants → extraction). That gets a readable narrative but leaves the triple
pipeline and the `_open_vars` hazard in place.

Duplication inside the engine, fixable during (or independent of) the split:

- [ ] **Effective-capacity computed four times** — `_hint_from_placement:998-1023`,
      `_add_crosslist_link_constraints:1816-1893`, `_add_capacity_constraints:2289-2375`,
      `_extract_solution:3005-3037` (the `capacity_mode == "room"` line is verbatim at
      999/1817/2291/3006). One `effective_capacity()` helper is the highest-value
      dedup in the file.
- [ ] **`re_solve` vs `repair_solve`** share a ~60-line skeleton including a verbatim
      15-line section-clone block (1394-1408 == 1491-1505); also `re_solve` loads pins
      with N per-section queries (1380-1391) while `repair_solve` bulk-loads
      (1460-1466). Extract `_load_base_schedule` + `_clone_sections`, unify on bulk.
      Pass pins/anchor as a `SolveContext` parameter instead of smuggling them through
      mutable instance attributes.
- [ ] **Model pays twice for "faculty teaches in slot"**: `_add_back_to_back_penalties`
      (2562-2577) rebuilds indicators `_add_faculty_constraints` already built
      (2084-2089). Share a faculty-in-slot cache on the builder.
- [x] The AND-linearization idiom (`2*b <= a1+a2; b >= a1+a2-1`) appears 5× (2087,
      2262, 2565, 2591, 2958) — name it once in `solver_util.py`.
      (2026-10-07: penalties._and_indicator, one direction per sign)
- [ ] `solve()` is 288 lines at four abstraction levels, including Rich TUI markup
      embedded in core solver code (1078, 1099, 1227, 1334 — which `generate.py:26`
      then regex-strips back out). Slice into build/configure/on-success/on-failure;
      move presentation markup to the callback layer.
- [ ] Readability inside methods: `_compute_section_in_slot` (2482) is the primitive
      ten methods consume, defined after nearly all its users; hard and soft builders
      interleave so file order teaches nothing; three different soft-term plumbing
      styles (unify on `(weight, var)`); 5-deep nesting + duplicated fallback in
      `_add_assignment_constraints` (1707-1724); hidden config knobs (`two_phase`,
      `placement_hint`, `faculty_load_limit`) absent from the config class defaults;
      repair weights defined in two places (109-114 vs 1542-1544); stale
      "backward compat" attrs `compatible_slots/rooms` (433-434) read nowhere.

**`finals.py` verdict: separation from the engine is correct — do not merge.** It
shares exactly the right amount (timeutil + solver_util). Minor: `_minutes_to_time`
(finals.py:51) belongs in timeutil; the block-overlap test is hand-written twice
(355-363, 507-515); `FinalsExporter` (640-792) could move out to mirror the
engine/export.py layout; `FinalsConfig` as a typed dataclass is a *better* pattern
than `SchedulerConfig`'s stringly nested dicts — copy it if SchedulerConfig is ever
reworked.

---

## P2 — API layer: shrink `routes/schedule.py` (996 lines, 15 sibling imports)

Layering is mostly healthy — the 996 lines are breadth (20 routes), not buried logic —
with four genuine leaks:

- [x] **Done 2026-07-21. `explain_section`**: the ~110-line body moved to
      `advisor.explain_section()`; the route is now a 3-line delegate and the
      route→route conflict-cache import is gone.
- [ ] **`pin_section` (`schedule.py:714-842`)**: the meeting-replacement semantics and
      partial-pin defaulting rules are business logic; extract to core (`apply_pin`),
      keep validation/shaping in the route. Pin/unpin also share a verbatim 15-line
      response tail (827-842 vs 867-883).
- [x] **Done 2026-07-21. Conflict-matrix cache** moved from `routes/conflicts.py` to
      `core/conflicts.py` (`get_conflict_matrix_cached`); the route re-imports it and
      the TUI can now share it.
- [ ] **Manual delete cascades duplicated across routers**: the finals cascade exists
      twice in different idioms (`crud.delete_schedule` bulk vs
      `finals.delete_finals_schedule` row-by-row). Move both to a shared home so the
      idioms can't diverge. *(Still open.)*
- [x] **Done 2026-07-21. Split `schedule.py` into a `routes/schedule/` package**:
      `crud.py` (238), `solve.py` (106), `pins.py` (196), `analysis.py` (283),
      `exports.py` (125), `__init__.py` combines the sub-routers + re-exports
      `get_session`. All 20 paths mount identically; 326 tests pass.
- [ ] **`database/queries.py` barely earns its existence**: 4 functions, 1 consumer,
      and `get_completed_courses` (queries.py:25-47) embeds the grade-order/passing
      rule — domain logic in the DB layer. Fold into core; let `src/database/` be
      models + connection only.

Consistency items (each small; fix opportunistically or in one sweep):

- [x] **Done 2026-07-21.** Job-lock: `job_lock.py` gained a canonical `BUSY_MESSAGE`
      and a `hold(kind, label)` context manager (`BusyError`). The four generic 409
      sites (generate/re-solve/repair/rebalance) and `finals.py` now use the one
      message; the `data.py` import path already holds the lock (P0). New call sites
      should prefer `hold()`; converting the existing try_acquire/finally sites is
      optional follow-up.
- [ ] No stated async-vs-sync rule: pin/unpin are `async def` doing blocking commits
      (`schedule.py:715, 846`), contradicting `delete_schedule`'s documented
      rationale 400 lines up. Simplest rule: plain `def` everywhere except SSE.
      State it once in `main.py` or `routes/__init__.py`.
- [ ] Schema location is split-brain: older schemas central in `schemas.py`, newer
      colocated. Colocated is winning — when schedule.py splits, move its schemas
      along and shrink `schemas.py` to genuinely shared types (it may disappear).
      Also: empty section headers (`schemas.py:176, 181, 186`), `SystemStats`
      misfiled under "Semester Guide Schemas" (:216), 17 `Optional[X]` vs 6 `X | None`.
- [ ] Three model-construction idioms; `FacultyInfo` hand-built identically three
      times (`data.py:351-361, 371-379, 426-434`) — dedupe at minimum.
- [ ] Error-shape asymmetries: blanket 500-wrappers in exactly three handlers with no
      comment saying why only those; `ValueError→404` without `from e` (9 sites);
      bad related-id = 400 but unknown import kind = 404. Pick lanes, note them.
- [ ] Filename sanitization regex duplicated (`schedule.py:623, 686`); finals CSV
      response inlines the pattern `_csv_download` was extracted for
      (`finals.py:314-329`).
- [ ] Stale text: `data.py:47-52` stats docstring describes the pre-P2 materialize-
      everything behavior in present tense; `data.py:22` assignment wedged between
      import blocks; `connection.py:112-133` decorative 19-name import list is
      already incomplete — replace with `import src.database.models  # noqa: F401`.
- [ ] Naming nits: module `recommend.py` vs prefix `/recommendations`; finals export
      `/export.csv` vs schedule `/export/banner`; `/api/health` (liveness) vs
      `/api/data/health` (doctor).

---

## P3 — Shared concepts trapped as privates (the recurring core-package debt)

Six cross-module underscore-imports exist because shared concepts never got a public
home. Same fix-shape as the P3-2026-07-14 consolidations (`timeutil`, `urgency`,
`xlsx_util`), which all worked:

- [x] **Done 2026-07-21. Academic calendar** → new `cadence.py` with public
      `is_off_cadence` / `next_term` / `derive_standing`. All six private
      cross-imports are gone: the four `_is_off_cadence`-from-engine importers and the
      two `next_term`/`derive_standing`-from-longrange importers now import from
      `cadence`. 326 tests pass.
- [x] **Done 2026-07-21. Scratch-DB sandbox** → new `scratchdb.py` with `backup_db`,
      `scratch_copy`, `delete_scratch` (the "never shutil, WAL sidecar" rationale
      stated once). The two duplicate `_scratch_copy` defs (scenarios, stress), the
      two inline backup blocks (longrange, replan), and the two cleanup blocks
      (stress, replan) all call it now; `profiles` imports from `scratchdb` instead of
      reaching into `scenarios` privates. What-if tests (34) pass.
- [x] **Done 2026-07-21. What-if library (partial)**: `scenarios._solve_copy` promoted
      to public `solve_copy`; `profiles` imports it (and `scratch_copy` from
      `scratchdb`) instead of reaching into `scenarios` privates. `stress._solve_variant`
      still mirrors `solve_copy`'s shape rather than sharing it — a fuller `whatif.py`
      consolidation is the remaining optional step.
- [x] **Done 2026-07-21.** `schedule_quality._analyze_student` → public
      `analyze_student` (seatswap imports it); `connection._REPO_DATA_DIR` → public
      `REPO_DATA_DIR` (doctor imports it). A stale test reference to the old private
      name was caught and fixed.
- [ ] **Banner import/export pairing**: evict `import_banner_schedule`
      (`ingest.py:894-1071`, 178 lines) — it writes *schedule* rows, a different
      layer from the reference-data loaders, and is explicitly not part of
      `load_all()`. A `banner.py` holding Banner import + `BannerExporter` would be
      self-describing. (Otherwise **keep `ingest.py` as one file** — ten parallel
      same-shaped loaders are cohesive; splitting would hurt.)
- [ ] **Read-side row assembly triplicated across consumers**: section display rows
      built independently by TUI (`tui/screens/schedule.py:141-190`) and API
      (`routes/schedule.py` get_schedule); finals assignment rows built three times
      (TUI `finals.py:186-221`, API `routes/finals.py:149-187`,
      `core/finals.py` exporter ~747). One core read-helper per case serves all
      three and ends the drift. The student-quality summary sentence is duplicated
      verbatim in two languages (`tui/screens/schedule.py:274-283` vs
      `ScheduleView.tsx:392-397`) and already had to be updated in lockstep once —
      have core emit labeled summary items.

---

## P4 — Frontend structure (web + TUI)

Web:

- [x] **Done 2026-07-21. Split `ScheduleView.tsx` (876 → 420)**: `ViabilityCard` and
      `QualityCard` extracted from the inline return, and the sibling components
      (`SolverQuality`, `DiffPanel`, `ReSolvePanel`, `ScheduleCalendar`,
      `ScheduleDetailTable` + its formatters) moved into a new `screens/schedule/`
      package. The `usePinning` / `useSectionMutations` hook extractions are the
      remaining optional step (deferred: stateful-hook moves are the one place a
      behavioral regression could hide with no frontend tests to catch it).
- [x] **Done 2026-07-21. Split `CalendarView.tsx` (707 → 467)**: pure logic
      (`buildEvents`, `layoutDay`, `snapSlot`, types — invariant comments carried
      verbatim) → `calendarLayout.ts`; the block renderer → `EventBlock.tsx`.
      `calendarLayout.ts` is now import-free of React and is the ideal first
      unit-test target — **recommended follow-up: add vitest + tests for `snapSlot`'s
      day-dominance rule and `layoutDay`'s lane assignment** (still zero frontend
      tests). The `useCalendarDrag` hook extraction was left in place for the same
      no-tests caution as the ScheduleView hooks.
- [ ] **Finish the `screens/` convention**: `screens/schedule/` now exists (holds the
      ScheduleView sub-components). Still open: moving the three single-consumer root
      components (`CalendarView`, `SectionDialog`, `ConflictPanel`) into it, and the
      fuller `lib/` + `components/` layout. Deferred as import-path churn with weak
      verification; the splits above already resolve the monolith concern.
- [ ] `api.ts`: dead `deleteFinals` wrapper (512-513, zero call sites); `importFile`
      re-implements `request()`'s error parsing verbatim (530-539 vs 46-53);
      stream-URL builders identical modulo a path segment (470-481); comment at
      430-432 describes an endpoint eight lines down. Strategic option, recorded not
      urgent: ~360 of 573 lines hand-mirror backend schemas — `openapi-typescript`
      against `/openapi.json` would delete the block and the drift risk.
- [ ] CSS: add `.banner-warn`/`.banner-info` and delete the three hand-inlined
      banner style blobs (`ScheduleView.tsx:439`, `FinalsView.tsx:260-267, 288-295`);
      move static inline styles (day header, ghost chip, legend swatch) to classes —
      grid-math styles stay inline.

TUI:

- [ ] **`src/tui/base.py`** — since P3 deleted `widgets/`, no shared TUI module
      exists, and the same boilerplate is written six times: cross-navigation
      BINDINGS + `action_switch_mode` + `action_quit` (all six screens — adding a
      screen today means editing six BINDINGS lists), the status-line helper ×3
      (two swallow exceptions, one doesn't), `EXPORT_DIR` + identical comment ×2,
      the job-lock guard + identical message ×3 (`dashboard.py:211-222`,
      `finals.py:256-267`, `schedule.py:305-312`), the newest-first list +
      index-clamp cycling ×2.
- [ ] `settings.py`: three parallel enumerations of the same six fields (key handler
      105-194, `_refresh_display` 68-84, `_update_prompt` 86-103) — table-driven
      spec or Textual `Input` widgets.
- [ ] `app.py:41-54`: six solver knobs as loose untyped attributes on the App, read
      via `self.app.solver_time` from screens — a small typed settings object
      restores checking and gives them one home.
- [ ] Copy-pair export actions (`schedule.py:332-366` vs `finals.py:340-366`, two
      tokens different); inconsistent lazy-import convention (`schedule.py:375-380`
      inline vs finals top-level).

---

## P5 — Signposting (cheapest, highest leverage — do first)

- [x] **Done 2026-07-21. Write the map.** The seven-layer inventory + the two
      conventions now live in `src/core/__init__.py`'s docstring, and the other four
      `__init__.py` files got package overviews. (Formerly all five were zero bytes.)
- [x] **Done 2026-07-21. Fix the misleading name pair** — via cross-referencing
      docstrings (lowest-churn option): `conflicts.py` now states it builds the
      pre-solve demand matrix (not schedule conflicts → see `validate.py`),
      `validate.py` says it finds conflicts in a SOLVED schedule (not `conflicts.py`),
      and `export.py` names itself the Banner exporter (iCal/finals live elsewhere).
      The renames and the `TimeSlotOverlapCalculator` move are still open if wanted.
- [x] **Done 2026-07-21. README code map** — a "Repository layout" section now points
      at the per-package maps.
- [x] **Done 2026-07-21. Claude.md architecture** — the diagram now includes the
      React web UI + SSE, and the stale ~50-line module tree was replaced with a
      compact accurate one that points at the package maps.
- [ ] Docstring upgrades to the standard the 2026-era modules set (`ical`, `stress`,
      `replan`, `uploads` are exemplary): **done for `demand.py`, `conflicts.py`,
      `export.py`** (2026-07-21); still open: `recommend.py`, `graph.py`, `ingest.py`.
      Killed the stray migrated-away comment at `degreeworks.py:47` ("same as
      demand.py" — scores moved to `urgency.py`).
- [ ] `sse.py`: add one sentence pinning that client disconnect deliberately does not
      cancel the worker.
- [ ] Repo hygiene: `exports/` mixes generator scripts, tracked deliverables, and
      untracked business documents (pptx/zip/md) in one directory — split
      generators from artifacts or gitignore the artifacts; stale `poetry.lock`
      and the finals-grid PDF at root still awaiting a decision
      (CODE_REVIEW.md flagged both).

Minor readability one-offs collected by the reviewers (batch into any nearby PR):
`ingest.py:353` walrus-in-ternary; `demand.py:379-585` 206-line function — the
guide-boost block (504-551) extracts cleanly; `validate.py:167-168` unreachable
guard; `export.py` two data-access idioms in one module (bulk at 102-118 vs per-
section loops at 274-285, 398-406) — same story in `recommend.py` (156-164 vs
217-220); `viability.py:92-99`/`schedule_quality.py:281-288`/`seatswap.py:98-104`
full-table scans + each rebuilds the same schedule-snapshot maps (extract a shared
snapshot loader before a fourth copy appears); `graph.py:95-96, 156-157` double
blank lines; `Dashboard.tsx:46` inline type import.

---

## Already good — do not churn

- **The layering itself**: clean DAG, no cycles; heavy importers are the
  orchestrators; `database.models` (40 importers) and `timeutil` (12) are the shared
  bases, as they should be.
- **The core conventions**: Session-first analyzers vs path-taking scratch runners;
  the uniform `emit`/`log` callback protocol; `generate.py` as the single
  SSE/emit orchestration seam.
- **The demand-sources design**: three sources, one urgency scale, one dispatch point
  (`generate.bootstrap_demands`).
- **The five-way assess family** (`doctor`/`validate`/`schedule_quality`/
  `viability`/`recommend`) — genuinely distinct jobs; a naming/map problem, not a
  merge candidate. **Do not consolidate.**
- **Engine internals worth preserving verbatim through any split**: the explicit
  `(model, X, Y, Z, section_in_slot)` builder signatures; `_compute_section_in_slot`
  shared indicators and the pairwise room/faculty constraints (the documented
  memory-explosion and false-infeasibility fixes); `_AssumptionProxy` +
  `INFEASIBILITY_HINTS` diagnosis (modulo the P0 fix); the why-docstrings
  (`_add_single_enrollment_constraints`, `_staffing_preflight`, repair weights).
- **`finals.py` engine separation**; `FinalsConfig`'s typed-dataclass pattern.
- **`sse.py`** (57 lines, textbook thread→loop marshalling), **`main.py`**,
  **`models.py` as one navigable file** (22 models, good dividers and field
  comments — splitting would hurt), **`connection.py`** (extract migrations only
  when the next migration lands), **`job_lock.py`** (extend, don't restructure).
- **Frontend**: `useSolverStream.ts` (intricate recovery state machine, excellent
  comments — the model for extracted hooks), `format.ts`/`useAsync.ts`/
  `components.tsx`/`optimizationStatus.tsx`, the single shared status poller via
  context, all small screens; **TUI** `on_screen_resume` reload + clamp pattern
  (correct and consistent — BaseScreen just gives it one home).
- **The P3-2026-07-14 consolidations** (`timeutil`, `urgency`, `xlsx_util`,
  `solver_util`) — each states its "one home for X" rationale; P3 above deliberately
  extends this pattern rather than inventing a new one.
- **`ingest.py`'s ten loaders as one file** and **`demand.py`'s two calculators** —
  right-sized; the only eviction is the Banner schedule importer.

---

## Suggested order of attack

1. **P0 bugs** — all five are small, independent diffs; do them first so refactors
   start from a correct base (and the test-import fix protects the engine split).
2. **P5 signposting** — the package map + README section + Claude.md fix is ~1 hour
   and makes every deferred item cheaper; do the `conflicts`/`validate` naming
   decision here too.
3. **Engine split** (P1) in commit-sized steps: `scheduler_config.py` (zero-risk) →
   `ScheduleModel` + hard constraints → penalties → warmstart → staffing;
   `effective_capacity()` and the re_solve/repair dedup can ride along.
4. **API layer** (P2): explain_section + conflict cache + pin logic to core (pure
   moves) → split `routes/schedule.py` → `job_lock.hold()` + cascades → mechanical
   consistency sweep.
5. **Frontend** (P4): ScheduleView hooks/components → CalendarView pure-logic
   extraction (+ first frontend unit tests) → folder moves → TUI `base.py` →
   settings table.
6. **P3 shared-concept promotions** as their areas get touched: `cadence.py` first
   (unblocks engine work), then `scratchdb`/`whatif`, then the row-assembly helpers
   (with the first shared regression tests for display rows).
