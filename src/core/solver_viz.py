"""Live visualization of an in-progress CP-SAT solve.

A schedule solve can run for ten minutes. With no feedback the UI looks
frozen, so users click away mid-run or mash keys in the TUI. This module
turns the search itself into a stream of small events that both frontends
render:

    viz_init   once, up front — the slot grid and the department legend
    incumbent  a new best schedule was found (carries the placement grid)
    bound      the proven bound moved (progress while the schedule sits still)
    activity   heartbeat — elapsed time, phase, whatever counters we have
    phase      the search entered a new phase

Everything reported here is *measured*. CP-SAT goes quiet for minutes at a
time while it proves a bound, and during those stretches the honest signals
are elapsed time, the incumbent count, and the age of the last improvement —
so those are what the events carry. Nothing here simulates motion to make
the screen look busy.

Two sources feed it:

* A solution callback, which is a stable OR-Tools API. It fires on every
  improved incumbent and gives us objective, bound, conflicts and branches.
* A tap on the solver's log stream, which is best-effort. The log's exact
  wording is not an API contract, so every field parsed out of it is
  optional and a parse miss degrades the display rather than breaking it.

The whole subsystem is opt-out (``--no-viz`` / ``GRADIMIZE_VIZ=0``). When
disabled, no callback is installed and the solve runs byte-identically to
how it ran before this module existed.
"""

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)

# emit(event_type, **fields) — the protocol already used by the SSE layer
# and by the TUI's worker thread.
Emit = Callable[..., None]

ENV_FLAG = "GRADIMIZE_VIZ"

# Cadence. A full placement snapshot is a few thousand Value() calls, so it
# is cheap but not free; throttling keeps a burst of early incumbents from
# stealing measurable time from the search.
MIN_GRID_INTERVAL = 0.4
HEARTBEAT_INTERVAL = 1.0
# Raw solver log lines are forwarded to the UI as a "machine room" tail. This
# is the only signal that distinguishes "CP-SAT is working" from "our stream
# is alive but the solver is wedged", so it is throttled for readability
# rather than suppressed.
SOLVER_LINE_INTERVAL = 0.3
MAX_LINE_CHARS = 160
# No improvement for this long and we relabel the phase: the solver has
# almost certainly stopped finding better schedules and is proving the bound.
QUIET_SECONDS = 20.0


def viz_enabled(override: Optional[bool] = None) -> bool:
    """Whether live visualization should run.

    An explicit argument (from ``--no-viz``) wins; otherwise the
    ``GRADIMIZE_VIZ`` environment variable decides. Default is on.
    """
    if override is not None:
        return bool(override)
    raw = os.environ.get(ENV_FLAG, "1").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def dept_of(course_code: str) -> str:
    """``BIO-101`` -> ``BIO``; the subject prefix used for grid colors."""
    code = (course_code or "").strip().upper()
    for sep in ("-", "_", " "):
        if sep in code:
            head = code.split(sep, 1)[0]
            if head:
                return head
    match = re.match(r"^[A-Z]+", code)
    return match.group(0) if match else (code or "?")


def _gap_pct(objective: float, bound: float) -> float:
    """Optimality gap, matching the formula used for the final stats."""
    return abs(bound - objective) / max(1.0, abs(objective)) * 100.0


class SolverViz:
    """Collects solver progress and emits UI events.

    Instances are touched from three threads — the solver's callback thread,
    the solver's logging thread, and our own heartbeat — so every mutation of
    shared state goes through ``_lock``. ``emit`` itself is expected to be
    thread-safe (both the SSE layer and the TUI marshal across threads).
    """

    def __init__(
        self,
        emit: Emit,
        sections: list,
        section_in_slot: dict,
        slots: list,
        time_limit: float,
    ) -> None:
        self._emit_raw = emit
        self._sis = section_in_slot
        self._time_limit = float(time_limit)

        # Department legend, ordered so colors stay stable across runs.
        labels: dict[str, str] = {}
        depts: dict[str, str] = {}
        for section in sections:
            crn = str(section.crn)
            labels[crn] = f"{section.course_code}-{section.section_number}"
            depts[crn] = dept_of(section.course_code)
        self._label = labels
        self._dept = depts
        self.dept_names = sorted(set(depts.values()))
        self._dept_idx = {
            crn: self.dept_names.index(dept) for crn, dept in depts.items()
        }

        self._slots = slots
        self._total_sections = len(sections)

        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._phase = "presolve"
        self._solutions = 0
        self._last_improvement = self._start
        self._last_grid_at = 0.0
        self._prev_placement: dict[str, frozenset] = {}
        self._objective: Optional[float] = None
        self._bound: Optional[float] = None
        self._conflicts: Optional[int] = None
        self._branches: Optional[int] = None
        self._restarts: Optional[int] = None
        self._log_lines = 0
        self._last_log: Optional[str] = None
        self._last_line_at = 0.0
        # Monotonic counter so the UI can advance a spinner one frame per
        # event received. Deriving it from real events (rather than a local
        # CSS/timer loop) means the animation stops if the stream stops —
        # motion on screen always corresponds to data actually arriving.
        self._ticks = 0

        self._stop = threading.Event()
        self._heartbeat: Optional[threading.Thread] = None

    # ── plumbing ──────────────────────────────────────────────────────

    def _emit(self, event_type: str, **fields: Any) -> None:
        """Emit, absorbing failures.

        This runs on solver threads. An exception escaping here would
        propagate into CP-SAT's callback machinery and abort the solve — a
        cosmetic feature must never be able to kill a ten-minute run.
        """
        try:
            self._emit_raw(event_type, **fields)
        except Exception:  # pragma: no cover - defensive
            logger.debug("viz emit failed", exc_info=True)

    def _elapsed(self) -> float:
        return time.monotonic() - self._start

    def _set_phase(self, phase: str) -> Optional[str]:
        """Record a phase change; returns the new phase if it changed."""
        if self._phase == phase:
            return None
        self._phase = phase
        return phase

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Emit the static payload and start the heartbeat."""
        self._emit(
            "viz_init",
            slots=[
                {
                    "id": slot.id,
                    "name": slot.name,
                    "pattern": slot.pattern,
                    "days": list(slot.days or []),
                    "start": slot.start_time,
                    "end": slot.end_time,
                }
                for slot in self._slots
            ],
            depts=self.dept_names,
            total_sections=self._total_sections,
            time_limit=self._time_limit,
        )
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="viz-heartbeat", daemon=True
        )
        self._heartbeat.start()

    def finish(self, status: str, **fields: Any) -> None:
        """Stop the heartbeat and emit a terminal phase event."""
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=2.0)
        with self._lock:
            elapsed = self._elapsed()
            solutions = self._solutions
        self._emit(
            "phase",
            phase="done",
            status=status,
            t=round(elapsed, 2),
            solutions=solutions,
            **fields,
        )

    def _heartbeat_loop(self) -> None:
        """Emit an activity tick so the UI always has honest motion.

        This is the one signal that cannot stall: even when CP-SAT reports
        nothing at all, elapsed time and the age of the last improvement are
        real, and they are what distinguishes "proving a bound" from "hung".
        """
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            with self._lock:
                elapsed = self._elapsed()
                quiet = time.monotonic() - self._last_improvement
                self._ticks += 1
                phase_changed = None
                if self._solutions and quiet >= QUIET_SECONDS:
                    phase_changed = self._set_phase("certifying")
                payload = {
                    "t": round(elapsed, 2),
                    "tick": self._ticks,
                    "phase": self._phase,
                    "solutions": self._solutions,
                    "quiet_for": round(quiet, 1) if self._solutions else None,
                    "objective": self._objective,
                    "bound": self._bound,
                    "conflicts": self._conflicts,
                    "branches": self._branches,
                    "restarts": self._restarts,
                    # log_lines rising is proof CP-SAT itself is still
                    # working; the tick above only proves we are.
                    "log_lines": self._log_lines,
                    "last_log": self._last_log,
                }
                if self._objective is not None and self._bound is not None:
                    payload["gap_pct"] = round(
                        _gap_pct(self._objective, self._bound), 2
                    )
            if phase_changed:
                self._emit("phase", phase=phase_changed, t=payload["t"])
            self._emit("activity", **payload)

    # ── solution callback ─────────────────────────────────────────────

    def on_solution(self, cb: cp_model.CpSolverSolutionCallback) -> None:
        """Handle one improved incumbent. Runs on a solver thread."""
        now = time.monotonic()
        with self._lock:
            self._solutions += 1
            n = self._solutions
            self._last_improvement = now
            phase_changed = self._set_phase(
                "first_solution" if n == 1 else "improving"
            )
            want_grid = (now - self._last_grid_at) >= MIN_GRID_INTERVAL
            if want_grid:
                self._last_grid_at = now

        try:
            objective = cb.ObjectiveValue()
            bound = cb.BestObjectiveBound()
        except Exception:  # pragma: no cover - defensive
            return

        payload: dict[str, Any] = {
            "n": n,
            "objective": objective,
            "bound": bound,
            "gap_pct": round(_gap_pct(objective, bound), 2),
            "t": round(self._elapsed(), 2),
        }
        for name, getter in (
            ("conflicts", cb.NumConflicts),
            ("branches", cb.NumBranches),
        ):
            try:
                payload[name] = getter()
            except Exception:  # pragma: no cover - defensive
                pass

        if want_grid:
            placed, moved = self._snapshot(cb)
            payload["placed"] = placed
            payload["moved"] = moved
            payload["placed_count"] = len(placed)

        with self._lock:
            self._objective = objective
            self._bound = bound
            self._conflicts = payload.get("conflicts", self._conflicts)
            self._branches = payload.get("branches", self._branches)

        if phase_changed:
            self._emit("phase", phase=phase_changed, t=payload["t"])
        self._emit("incumbent", **payload)

    def _snapshot(self, cb: cp_model.CpSolverSolutionCallback):
        """Build the placement grid and count how many sections moved.

        Scans the ``(crn, k, slot_id) -> BoolVar`` indicators rather than the
        full room-level X dict: same information for a day/time grid, but
        one to two orders of magnitude fewer Value() calls.
        """
        placed: list[list[Any]] = []
        current: dict[str, set] = {}
        for (crn, _k, slot_id), var in self._sis.items():
            try:
                if not cb.Value(var):
                    continue
            except Exception:  # pragma: no cover - defensive
                continue
            placed.append([slot_id, self._dept_idx.get(crn, 0), self._label.get(crn, crn)])
            current.setdefault(crn, set()).add(slot_id)

        frozen = {crn: frozenset(slots) for crn, slots in current.items()}
        if self._prev_placement:
            keys = set(frozen) | set(self._prev_placement)
            moved = sum(
                1 for k in keys if frozen.get(k) != self._prev_placement.get(k)
            )
        else:
            moved = len(frozen)
        self._prev_placement = frozen
        return placed, moved

    # ── log tap ───────────────────────────────────────────────────────

    # CP-SAT progress lines look like:
    #   #12     4.60s best:8450  next:[8460,8700]  fixed_bools:0/12345
    #   #Bound  9.31s best:8450  next:[8500,8690]
    # The wording is not an API contract, so each field is parsed
    # independently and any miss is simply skipped.
    _RE_NEXT = re.compile(r"next:\s*\[\s*(-?[\d.eE+]+)\s*,\s*(-?[\d.eE+]+)\s*\]")
    _RE_BEST = re.compile(r"best:\s*(-?[\d.eE+]+)")
    _RE_RESTARTS = re.compile(r"restarts:\s*(\d+)")
    _RE_CONFLICTS = re.compile(r"conflicts:\s*(\d+)")
    # CP-SAT announces the presolve->search handoff, e.g.
    #   "Starting search at 46.86s with 60 workers."
    # Presolve on a full-term model can run for the better part of a minute,
    # so without this the UI would claim "Setting up" while 60 workers are
    # already searching. Best-effort like the rest of the log tap.
    _RE_SEARCH_START = re.compile(
        r"Starting search at\s+([\d.]+)s\s+with\s+(\d+)\s+workers"
    )

    def on_log_line(self, line: str) -> None:
        """Consume one CP-SAT log line. Best-effort, never raises."""
        try:
            self._parse_log_line(line)
        except Exception:  # pragma: no cover - defensive
            logger.debug("viz log parse failed", exc_info=True)

    def _parse_log_line(self, line: str) -> None:
        bound: Optional[float] = None
        forward: Optional[str] = None
        phase_changed: Optional[str] = None
        workers: Optional[int] = None
        with self._lock:
            self._log_lines += 1

            text = " ".join(line.split())[:MAX_LINE_CHARS]
            if text:
                self._last_log = text
                now = time.monotonic()
                if (now - self._last_line_at) >= SOLVER_LINE_INTERVAL:
                    self._last_line_at = now
                    self._ticks += 1
                    forward = text
                    tick = self._ticks
                    lines_seen = self._log_lines

            match = self._RE_NEXT.search(line)
            if match:
                low, high = float(match.group(1)), float(match.group(2))
                # Maximization: the bound is the optimistic end of the range.
                candidate = max(low, high)
                if self._bound is None or candidate != self._bound:
                    self._bound = candidate
                    bound = candidate

            match = self._RE_BEST.search(line)
            if match:
                try:
                    self._objective = float(match.group(1))
                except ValueError:
                    pass

            match = self._RE_SEARCH_START.search(line)
            if match and self._phase == "presolve":
                phase_changed = self._set_phase("searching")
                workers = int(match.group(2))

            for regex, attr in (
                (self._RE_RESTARTS, "_restarts"),
                (self._RE_CONFLICTS, "_conflicts"),
            ):
                match = regex.search(line)
                if match:
                    setattr(self, attr, int(match.group(1)))

            objective = self._objective
            elapsed = self._elapsed()

        if phase_changed:
            self._emit(
                "phase", phase=phase_changed, workers=workers,
                t=round(elapsed, 2),
            )

        # The raw line is the most direct "machinery at work" signal there
        # is: it changes only when CP-SAT itself writes something, so a
        # stalled tail means a stalled solver, not just a stalled UI.
        if forward is not None:
            self._emit(
                "solver_line",
                line=forward,
                tick=tick,
                log_lines=lines_seen,
                t=round(elapsed, 2),
            )

        # A moving bound is the main evidence of progress while the
        # incumbent sits still, so it gets its own event rather than
        # waiting for the next heartbeat.
        if bound is not None:
            payload: dict[str, Any] = {"bound": bound, "t": round(elapsed, 2)}
            if objective is not None:
                payload["objective"] = objective
                payload["gap_pct"] = round(_gap_pct(objective, bound), 2)
            self._emit("bound", **payload)


class _VizCallback(cp_model.CpSolverSolutionCallback):
    """Thin adapter so ``SolverViz`` stays independent of OR-Tools types."""

    def __init__(self, viz: SolverViz) -> None:
        super().__init__()
        self._viz = viz

    def on_solution_callback(self) -> None:  # noqa: N802 - OR-Tools API
        self._viz.on_solution(self)


def attach(
    solver: cp_model.CpSolver,
    emit: Optional[Emit],
    sections: list,
    section_in_slot: dict,
    slots: list,
    time_limit: float,
    enabled: Optional[bool] = None,
) -> tuple[Optional[SolverViz], Optional[cp_model.CpSolverSolutionCallback]]:
    """Wire live visualization into a configured solver.

    Returns ``(viz, callback)``; both are ``None`` when visualization is off
    or unavailable, in which case the caller solves exactly as before. The
    caller is responsible for passing the callback to ``Solve`` and for
    calling ``viz.finish(...)`` afterwards.
    """
    if emit is None or not viz_enabled(enabled):
        return None, None
    if not section_in_slot:
        return None, None

    viz = SolverViz(emit, sections, section_in_slot, slots, time_limit)

    # Route the solver's log into the tap instead of stdout. Without this the
    # verbose log garbles the TUI, which is why it used to be switched off
    # entirely; captured, it becomes the bound/counter feed.
    try:
        solver.parameters.log_search_progress = True
        solver.parameters.log_to_stdout = False
        solver.log_callback = viz.on_log_line
    except Exception:  # pragma: no cover - older OR-Tools
        logger.debug("solver log tap unavailable", exc_info=True)

    viz.start()
    return viz, _VizCallback(viz)
