"""Tests for the live solver visualization event stream.

These exercise `SolverViz` directly with stub solver objects rather than
running CP-SAT: the point is the event contract the frontends rely on
(payload shapes, throttling, phase transitions) and the guarantee that a
cosmetic feature can never abort a real solve.
"""

import threading
import time
import types

import pytest

from src.core import solver_viz
from src.core.solver_viz import SolverViz, attach, dept_of, viz_enabled


class FakeSection:
    def __init__(self, crn, course_code, section_number):
        self.crn = crn
        self.course_code = course_code
        self.section_number = section_number


class FakeSlot:
    def __init__(self, id, name, pattern, days, start, end):
        self.id = id
        self.name = name
        self.pattern = pattern
        self.days = days
        self.start_time = start
        self.end_time = end


class FakeCallback:
    """Duck-typed stand-in for CpSolverSolutionCallback."""

    def __init__(self, values, objective=100.0, bound=120.0):
        self.values = values
        self._objective = objective
        self._bound = bound

    def Value(self, var):
        return self.values.get(var, 0)

    def ObjectiveValue(self):
        return self._objective

    def BestObjectiveBound(self):
        return self._bound

    def NumConflicts(self):
        return 4321

    def NumBranches(self):
        return 8765


class Recorder:
    """Thread-safe emit sink."""

    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def __call__(self, event_type, **fields):
        with self._lock:
            self.events.append((event_type, fields))

    def of(self, event_type):
        with self._lock:
            return [f for t, f in self.events if t == event_type]

    def types(self):
        with self._lock:
            return [t for t, _ in self.events]


SECTIONS = [
    FakeSection("1", "BIO-101", "001"),
    FakeSection("2", "CHM-205", "001"),
    FakeSection("3", "BIO-101", "002"),
]

SLOTS = [
    FakeSlot(1, "MWF_0800", "MWF", ["M", "W", "F"], "08:00", "08:50"),
    FakeSlot(2, "TR_0930", "TR", ["T", "R"], "09:30", "10:45"),
]


def make_viz(emit, sis=None):
    sis = sis if sis is not None else {
        ("1", 0, 1): "v1",
        ("2", 0, 2): "v2",
        ("3", 0, 1): "v3",
    }
    return SolverViz(emit, SECTIONS, sis, SLOTS, time_limit=600.0)


# ── flag handling ────────────────────────────────────────────────────


def test_viz_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv(solver_viz.ENV_FLAG, raising=False)
    assert viz_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", "  "])
def test_viz_disabled_by_env(monkeypatch, value):
    monkeypatch.setenv(solver_viz.ENV_FLAG, value)
    assert viz_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_viz_enabled_by_env(monkeypatch, value):
    monkeypatch.setenv(solver_viz.ENV_FLAG, value)
    assert viz_enabled() is True


def test_explicit_override_beats_env(monkeypatch):
    monkeypatch.setenv(solver_viz.ENV_FLAG, "0")
    assert viz_enabled(True) is True
    monkeypatch.delenv(solver_viz.ENV_FLAG, raising=False)
    assert viz_enabled(False) is False


@pytest.mark.parametrize(
    "code,expected",
    [
        ("BIO-101", "BIO"),
        ("CHM_205", "CHM"),
        ("MATH200", "MATH"),
        ("bio-101", "BIO"),
        ("ENGL 110", "ENGL"),
        ("", "?"),
        # No letter prefix: fall back to the code itself rather than
        # collapsing every odd code into one shared color.
        ("123", "123"),
    ],
)
def test_dept_of(code, expected):
    assert dept_of(code) == expected


# ── attach() opt-out path ────────────────────────────────────────────


def _fake_solver():
    return types.SimpleNamespace(
        parameters=types.SimpleNamespace(
            log_search_progress=False, log_to_stdout=True
        ),
        log_callback=None,
    )


def test_attach_noop_without_emitter():
    viz, cb = attach(_fake_solver(), None, SECTIONS, {("1", 0, 1): "v"},
                     SLOTS, 60.0)
    assert viz is None and cb is None


def test_attach_noop_when_disabled():
    viz, cb = attach(_fake_solver(), Recorder(), SECTIONS, {("1", 0, 1): "v"},
                     SLOTS, 60.0, enabled=False)
    assert viz is None and cb is None


def test_attach_noop_without_indicators():
    """No slot indicators means nothing to draw — stay out of the way."""
    viz, cb = attach(_fake_solver(), Recorder(), SECTIONS, {}, SLOTS, 60.0)
    assert viz is None and cb is None


def test_attach_redirects_solver_log(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    solver = _fake_solver()
    rec = Recorder()
    viz, cb = attach(solver, rec, SECTIONS, {("1", 0, 1): "v"}, SLOTS, 60.0,
                     enabled=True)
    try:
        assert viz is not None and cb is not None
        # The log must be captured, never printed: stdout logging is what
        # used to garble the TUI.
        assert solver.parameters.log_search_progress is True
        assert solver.parameters.log_to_stdout is False
        assert solver.log_callback is not None
        assert rec.of("viz_init")
    finally:
        viz.finish("optimal")


# ── viz_init payload ─────────────────────────────────────────────────


def test_viz_init_carries_legend_and_slots(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    rec = Recorder()
    viz = make_viz(rec)
    viz.start()
    try:
        init = rec.of("viz_init")[0]
        assert init["depts"] == ["BIO", "CHM"]
        assert init["total_sections"] == 3
        assert init["time_limit"] == 600.0
        slot = init["slots"][0]
        assert slot["id"] == 1
        assert slot["days"] == ["M", "W", "F"]
        assert slot["start"] == "08:00"
    finally:
        viz.finish("optimal")


# ── incumbents ───────────────────────────────────────────────────────


def test_incumbent_payload_and_placement(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    rec = Recorder()
    viz = make_viz(rec)
    cb = FakeCallback({"v1": 1, "v2": 1, "v3": 0}, objective=100.0, bound=110.0)
    viz.on_solution(cb)

    inc = rec.of("incumbent")[0]
    assert inc["n"] == 1
    assert inc["objective"] == 100.0
    assert inc["bound"] == 110.0
    assert inc["gap_pct"] == pytest.approx(10.0)
    assert inc["conflicts"] == 4321
    assert inc["branches"] == 8765
    assert inc["placed_count"] == 2

    placed = sorted(inc["placed"])
    assert placed == [[1, 0, "BIO-101-001"], [2, 1, "CHM-205-001"]]
    # Every section is "moved" on the first snapshot — there is no prior grid.
    assert inc["moved"] == 2


def test_moved_counts_only_changed_sections(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "MIN_GRID_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)

    viz.on_solution(FakeCallback({"v1": 1, "v2": 1, "v3": 0}))
    # Section 3 enters; sections 1 and 2 stay put.
    viz.on_solution(FakeCallback({"v1": 1, "v2": 1, "v3": 1}))

    assert rec.of("incumbent")[1]["moved"] == 1


def test_grid_snapshots_are_throttled(monkeypatch):
    """A burst of early incumbents must not each pay for a full scan."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "MIN_GRID_INTERVAL", 60)
    rec = Recorder()
    viz = make_viz(rec)

    viz.on_solution(FakeCallback({"v1": 1}))
    viz.on_solution(FakeCallback({"v1": 1, "v2": 1}))

    first, second = rec.of("incumbent")
    assert "placed" in first
    # Throttled: no grid, but the convergence numbers still arrive so the
    # chart and gap badge stay accurate.
    assert "placed" not in second
    assert second["objective"] == 100.0
    assert second["gap_pct"] == pytest.approx(20.0)


def test_phase_transitions(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "MIN_GRID_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)

    viz.on_solution(FakeCallback({"v1": 1}))
    viz.on_solution(FakeCallback({"v1": 1, "v2": 1}))
    viz.on_solution(FakeCallback({"v2": 1}))

    phases = [f["phase"] for f in rec.of("phase")]
    # Announced once on entry, not repeated per solution.
    assert phases == ["first_solution", "improving"]


def test_certifying_phase_after_quiet_period(monkeypatch):
    """The long silent stretch must be labelled, not left looking hung."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(solver_viz, "QUIET_SECONDS", 0.05)
    monkeypatch.setattr(solver_viz, "MIN_GRID_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)
    viz.start()
    try:
        viz.on_solution(FakeCallback({"v1": 1}))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any(f["phase"] == "certifying" for f in rec.of("phase")):
                break
            time.sleep(0.02)
        assert any(f["phase"] == "certifying" for f in rec.of("phase"))
        act = rec.of("activity")[-1]
        assert act["solutions"] == 1
        assert act["quiet_for"] is not None
    finally:
        viz.finish("feasible")


def test_heartbeat_emits_activity_and_stops_on_finish(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 0.02)
    rec = Recorder()
    viz = make_viz(rec)
    viz.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not rec.of("activity"):
        time.sleep(0.02)
    assert rec.of("activity"), "heartbeat produced no activity events"

    viz.finish("optimal")
    settled = len(rec.of("activity"))
    time.sleep(0.1)
    assert len(rec.of("activity")) == settled, "heartbeat kept running"

    done = rec.of("phase")[-1]
    assert done["phase"] == "done"
    assert done["status"] == "optimal"


# ── log tap ──────────────────────────────────────────────────────────


def test_log_line_emits_bound(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("#Bound  9.31s best:8450 next:[8500,8690]")

    bound = rec.of("bound")[0]
    assert bound["bound"] == 8690.0
    assert bound["objective"] == 8450.0
    assert bound["gap_pct"] == pytest.approx(2.84, abs=0.01)


def test_repeated_bound_is_not_re_emitted(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("#Bound 1.0s best:100 next:[100,120]")
    viz.on_log_line("#Bound 2.0s best:100 next:[100,120]")
    assert len(rec.of("bound")) == 1


def test_search_start_leaves_presolve(monkeypatch):
    """CP-SAT presolve can run ~47s on a full term; don't call that "setting
    up" once 60 workers are actually searching."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "SOLVER_LINE_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("Presolve summary:")
    assert rec.of("phase") == []

    viz.on_log_line("Starting search at 46.86s with 60 workers.")
    phase = rec.of("phase")[0]
    assert phase["phase"] == "searching"
    assert phase["workers"] == 60

    # Announced once, not on every later line.
    viz.on_log_line("Starting search at 46.86s with 60 workers.")
    assert len(rec.of("phase")) == 1


def test_first_solution_supersedes_searching(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "SOLVER_LINE_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("Starting search at 1.0s with 8 workers.")
    viz.on_solution(FakeCallback({"v1": 1}))
    assert [f["phase"] for f in rec.of("phase")] == ["searching", "first_solution"]


def test_solver_line_forwards_raw_log(monkeypatch):
    """The raw log tail is the only proof CP-SAT itself is still working."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "SOLVER_LINE_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("#Bound   9.31s    best:8450   next:[8500,8690]")

    line = rec.of("solver_line")[0]
    assert line["line"] == "#Bound 9.31s best:8450 next:[8500,8690]"
    assert line["log_lines"] == 1
    assert line["tick"] >= 1


def test_solver_line_is_throttled_but_tail_stays_current(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "SOLVER_LINE_INTERVAL", 60)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("#1 first")
    viz.on_log_line("#2 second")
    viz.on_log_line("#3 third")

    assert len(rec.of("solver_line")) == 1
    # Throttling must not make the tail stale: the next heartbeat still
    # reports the newest line the solver produced.
    assert viz._last_log == "#3 third"


def test_solver_line_is_truncated(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    monkeypatch.setattr(solver_viz, "SOLVER_LINE_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("#" + "x" * 500)
    assert len(rec.of("solver_line")[0]["line"]) == solver_viz.MAX_LINE_CHARS


def test_activity_carries_tail_and_advancing_tick(monkeypatch):
    """Tick advances only on real events, so a dead stream stops the spinner."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(solver_viz, "SOLVER_LINE_INTERVAL", 0)
    rec = Recorder()
    viz = make_viz(rec)
    viz.on_log_line("#Bound 1.0s best:100 next:[100,120]")
    viz.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(rec.of("activity")) < 2:
            time.sleep(0.02)
        acts = rec.of("activity")
        assert len(acts) >= 2, "heartbeat did not produce activity events"
        assert acts[-1]["last_log"] == "#Bound 1.0s best:100 next:[100,120]"
        assert acts[-1]["log_lines"] == 1
        assert acts[-1]["tick"] > acts[0]["tick"]
    finally:
        viz.finish("feasible")


def test_unparseable_log_lines_are_harmless(monkeypatch):
    """The CP-SAT log wording is not an API contract; a miss must degrade."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    rec = Recorder()
    viz = make_viz(rec)
    for line in ["", "Starting search", "#1 0.05s", "garbage next:[oops]", "\x00"]:
        viz.on_log_line(line)
    assert rec.of("bound") == []


# ── robustness ───────────────────────────────────────────────────────


def test_emit_failures_never_reach_the_solver(monkeypatch):
    """A broken UI stream must not abort a ten-minute solve."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)

    def exploding_emit(event_type, **fields):
        raise RuntimeError("client disconnected")

    viz = make_viz(exploding_emit)
    viz.on_solution(FakeCallback({"v1": 1}))
    viz.on_log_line("#Bound 1.0s best:100 next:[100,120]")
    viz.finish("optimal")  # must not raise


def test_broken_callback_never_reaches_the_solver(monkeypatch):
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    rec = Recorder()
    viz = make_viz(rec)

    class Broken(FakeCallback):
        def ObjectiveValue(self):
            raise RuntimeError("solver went away")

    viz.on_solution(Broken({"v1": 1}))  # must not raise


def test_unknown_variable_values_are_skipped(monkeypatch):
    """A Value() that blows up on one var must not lose the whole grid."""
    monkeypatch.setattr(solver_viz, "HEARTBEAT_INTERVAL", 30)
    rec = Recorder()
    viz = make_viz(rec)

    class Picky(FakeCallback):
        def Value(self, var):
            if var == "v2":
                raise RuntimeError("stale var")
            return self.values.get(var, 0)

    viz.on_solution(Picky({"v1": 1, "v2": 1, "v3": 1}))
    placed = rec.of("incumbent")[0]["placed"]
    assert sorted(p[2] for p in placed) == ["BIO-101-001", "BIO-101-002"]
