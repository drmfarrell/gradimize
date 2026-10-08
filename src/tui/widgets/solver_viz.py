"""Live solver-progress panel for the TUI dashboard.

Renders the same event stream produced by ``src/core/solver_viz.py`` (see
that module for the protocol). The rendering logic is split into small pure
functions — string in, string out — so it can be unit tested without a
running Textual app. ``SolverVizPanel`` is the thin, thread-safe widget that
calls them and pushes the results onto the screen.

The panel starts hidden and only becomes visible once a ``viz_init`` event
arrives. If visualization is disabled on the solver side, no such event is
ever sent and the panel — and the rest of the dashboard — looks exactly as
it did before this module existed.
"""

import time
from collections import deque
from typing import Any, Optional, Sequence

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

# Must match the web palette exactly, same order.
PALETTE = [
    "#E69F00", "#56B4E9", "#009E73", "#F0E442",
    "#0072B2", "#D55E00", "#CC79A7", "#999999",
    "#8DD3C7", "#BC80BD", "#B15928", "#7FC97F",
]

PHASE_LABELS = {
    "presolve": "Setting up",
    "searching": "Searching",
    "first_solution": "First schedule found",
    "improving": "Improving",
    "certifying": "Certifying (proving optimality)",
    "done": "Done",
}

DAYS_ORDER = ["M", "T", "W", "R", "F"]

SPARK_WIDTH = 60
SPARK_CHARS = " ▁▂▃▄▅▆▇█"

# Braille spinner frames; the frame shown is derived from the solver's own
# `tick` counter (never a timer), so motion stops the instant the event
# stream does.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

TAIL_MAX_LINES = 4
LINE_BUFFER_CAP = 10
STALE_THRESHOLD = 5.0


# ── pure helpers ─────────────────────────────────────────────────────────


def _fmt_mmss(seconds: Optional[float]) -> str:
    """``M:SS``, clamped at zero. Negative or missing input reads as 0:00."""
    total = int(round(seconds or 0))
    if total < 0:
        total = 0
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def format_progress_bar(
    t: float,
    time_limit: float,
    phase: str,
    gap_pct: Optional[float] = None,
    width: int = 30,
    workers: Optional[int] = None,
) -> str:
    """Render the top progress bar as a single line of text.

    The fill fraction is clamped to [0, 1] regardless of how far ``t``
    strays outside ``[0, time_limit]``; the displayed elapsed time is not
    clamped upward (it is honest even once past the limit) but is floored
    at zero. When ``phase`` is ``"searching"`` and ``workers`` is known,
    the label reads ``"Searching · N workers"``.
    """
    label = PHASE_LABELS.get(phase, phase or "")
    if phase == "searching" and workers is not None:
        label = f"{label} · {workers} workers"
    if time_limit and time_limit > 0:
        frac = t / time_limit
    else:
        frac = 0.0
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    bar = "█" * filled + "░" * (width - filled)
    parts = [label, f"[{bar}]", f"{_fmt_mmss(t)} / {_fmt_mmss(time_limit)}"]
    if gap_pct is not None:
        parts.append(f"gap {gap_pct:.1f}%")
    return "  ".join(p for p in parts if p)


def _spark_line(values: Sequence[float], width: int = SPARK_WIDTH) -> str:
    vals = list(values)[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    rng = hi - lo
    if rng == 0:
        return SPARK_CHARS[4] * len(vals)
    out = []
    for v in vals:
        idx = int(round((v - lo) / rng * (len(SPARK_CHARS) - 1)))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def render_sparkline(
    objective_series: Sequence[float],
    bound_series: Optional[Sequence[float]] = None,
    width: int = SPARK_WIDTH,
) -> str:
    """A one- or two-line sparkline: objective, then bound if present."""
    obj_line = _spark_line(objective_series, width)
    if not obj_line:
        return "(no data yet)"
    lines = [f"obj  {obj_line}"]
    if bound_series:
        bound_line = _spark_line(bound_series, width)
        if bound_line:
            lines.append(f"bnd  {bound_line}")
    return "\n".join(lines)


def format_activity_line(solutions: Optional[int], quiet_for: Optional[float]) -> str:
    """``N solutions · last improved 0:38 ago``, omitting unknown fields."""
    if solutions is None:
        return ""
    parts = [f"{solutions} solutions"]
    if quiet_for is not None:
        parts.append(f"last improved {_fmt_mmss(quiet_for)} ago")
    return " · ".join(parts)


def format_spinner(tick: Optional[int]) -> str:
    """One braille spinner frame, derived from ``tick`` only.

    ``tick % len(SPINNER_FRAMES)`` — no timer, no wall clock. Motion stops
    the moment the event stream stops because nothing new advances the
    frame. ``tick=None`` (no event with a tick has arrived yet) renders a
    neutral placeholder rather than a moving frame.
    """
    if tick is None:
        return "·"
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]


def format_stale(seconds_since_last: Optional[float], threshold: float = STALE_THRESHOLD) -> str:
    """``"no signal for Ns"`` once the gap since the last event exceeds
    ``threshold``; empty string otherwise (including when unknown)."""
    if seconds_since_last is None or seconds_since_last <= threshold:
        return ""
    return f"no signal for {int(seconds_since_last)}s"


def render_tail(
    lines: Sequence[str],
    width: int = 80,
    max_lines: int = TAIL_MAX_LINES,
) -> str:
    """The "machine room" tail: last ``max_lines`` raw log lines.

    Newest at the bottom (matching arrival order), each line truncated —
    never wrapped — to ``width`` characters. Every line but the newest is
    dimmed via Rich markup.
    """
    items = [str(line)[:width] for line in list(lines)[-max_lines:]]
    if not items:
        return "(no output yet)"
    last = len(items) - 1
    return "\n".join(
        line if i == last else f"[dim]{line}[/dim]" for i, line in enumerate(items)
    )


def _block(dept_idx: int) -> str:
    color = PALETTE[dept_idx % len(PALETTE)]
    return f"[{color}]█[/]"


def _render_cell(entries: list, width: int) -> str:
    n = len(entries)
    if n == 0:
        return " " * width
    if n <= width:
        body = "".join(_block(d) for d, _ in entries)
        return body + " " * (width - n)
    # Overflow: truncate and append "+N", shrinking the shown count until
    # the block run plus the suffix fits the cell width.
    for shown in range(width, -1, -1):
        suffix = f"+{n - shown}"
        if shown + len(suffix) <= width:
            body = "".join(_block(d) for d, _ in entries[:shown])
            pad = width - shown - len(suffix)
            return body + suffix + " " * pad
    suffix = f"+{n}"
    return suffix[:width].ljust(width)


def _render_legend(depts: Sequence[str]) -> str:
    if not depts:
        return ""
    items = [f"{_block(i)} {d}" for i, d in enumerate(depts)]
    return "Legend: " + "  ".join(items)


TIME_COL_WIDTH = 13


def render_grid_placeholder(phase: str, workers: Optional[int] = None) -> str:
    """The dead-space message shown before any schedule has been placed.

    Presolve on a real-size model can run for tens of seconds with nothing
    to draw; an empty grid reads as "frozen", so this fills that space
    honestly instead. Not animated — the spinner and console tail already
    carry liveness.
    """
    if phase == "presolve":
        return "Simplifying the problem — no schedule to show yet"
    if phase == "searching":
        base = "Searching for a first workable schedule"
        return f"{base} ({workers} workers)" if workers else base
    return "Waiting for the first schedule"


def render_grid(
    slots: Sequence[dict],
    depts: Sequence[str],
    placed: Sequence[Sequence[Any]],
    cell_width: int = 8,
    placeholder: Optional[str] = None,
) -> str:
    """The department-colored week grid.

    Rows are distinct time slots (deduplicated by start+end, sorted by
    start); columns are M T W R F. A slot's placements appear in every
    column named in that slot's ``days`` list. ``placed`` items are
    ``[slot_id, dept_idx, label]``, matching the ``incumbent.placed``
    payload.

    When ``placeholder`` is given (no schedule to show yet), the header
    row and per-slot row count are kept identical to a normal render —
    only the middle row's cells are replaced with the centered message —
    so the panel does not change height once the first schedule lands.
    """
    by_slot: dict[Any, list] = {}
    for item in placed:
        if len(item) < 3:
            continue
        slot_id, dept_idx, label = item[0], item[1], item[2]
        by_slot.setdefault(slot_id, []).append((dept_idx, label))

    seen: dict[tuple, dict] = {}
    rows: list[dict] = []
    for slot in sorted(slots, key=lambda s: s.get("start") or ""):
        key = (slot.get("start"), slot.get("end"))
        if key not in seen:
            seen[key] = {"start": slot.get("start"), "end": slot.get("end"), "slots": []}
            rows.append(seen[key])
        seen[key]["slots"].append(slot)

    row_width = cell_width * len(DAYS_ORDER) + 2 * (len(DAYS_ORDER) - 1)
    lines = [
        "Time".ljust(TIME_COL_WIDTH)
        + "  ".join(f"{d:^{cell_width}}" for d in DAYS_ORDER)
    ]

    mid_idx = len(rows) // 2 if rows else None
    for i, row in enumerate(rows):
        time_label = f"{row['start']}-{row['end']}"
        if placeholder is not None and i == mid_idx:
            lines.append(
                " " * TIME_COL_WIDTH + f"[dim]{placeholder.center(row_width)}[/dim]"
            )
            continue
        cells: dict[str, list] = {d: [] for d in DAYS_ORDER}
        if placeholder is None:
            for slot in row["slots"]:
                entries = by_slot.get(slot.get("id"), [])
                for d in slot.get("days") or []:
                    if d in cells:
                        cells[d].extend(entries)
        cell_strs = [_render_cell(cells[d], cell_width) for d in DAYS_ORDER]
        lines.append(f"{time_label:<{TIME_COL_WIDTH}}" + "  ".join(cell_strs))

    if placeholder is not None and not rows:
        lines.append(f"[dim]{placeholder}[/dim]")

    legend = _render_legend(depts)
    if legend:
        lines.append(legend)
    return "\n".join(lines)


def format_footer(
    solutions: Optional[int] = None,
    moved: Optional[int] = None,
    conflicts: Optional[int] = None,
    branches: Optional[int] = None,
    restarts: Optional[int] = None,
    log_lines: Optional[int] = None,
    elapsed: Optional[float] = None,
) -> str:
    """Activity footer line, omitting any field that is ``None``."""
    parts = []
    if solutions is not None:
        parts.append(f"solutions {solutions}")
    if moved is not None:
        parts.append(f"moved {moved}")
    if conflicts is not None:
        parts.append(f"conflicts {conflicts}")
    if branches is not None:
        parts.append(f"branches {branches}")
    if restarts is not None:
        parts.append(f"restarts {restarts}")
    if log_lines is not None:
        parts.append(f"output {log_lines:,} lines")
    if elapsed is not None:
        parts.append(f"elapsed {_fmt_mmss(elapsed)}")
    return " · ".join(parts)


# ── widget ────────────────────────────────────────────────────────────────


class SolverVizPanel(Vertical):
    """Container widget rendering the four live-solve regions.

    Hidden until a ``viz_init`` event arrives. ``handle_event`` may be
    called from any thread (it is what solver threads call via the
    dashboard's ``emit``); it marshals onto the main thread before
    touching any widget, matching the existing ``_write_log`` pattern.
    """

    DEFAULT_CSS = """
    SolverVizPanel {
        height: auto;
        display: none;
    }
    SolverVizPanel Static {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._time_limit = 0.0
        self._depts: list[str] = []
        self._slots: list[dict] = []
        self._objective_series: deque = deque(maxlen=SPARK_WIDTH)
        self._bound_series: deque = deque(maxlen=SPARK_WIDTH)
        self._last_placed: list = []
        self._phase = "presolve"
        self._t = 0.0
        self._gap_pct: Optional[float] = None
        self._solutions: Optional[int] = None
        self._moved: Optional[int] = None
        self._conflicts: Optional[int] = None
        self._branches: Optional[int] = None
        self._restarts: Optional[int] = None
        self._quiet_for: Optional[float] = None
        self._tick: Optional[int] = None
        self._log_lines: Optional[int] = None
        self._lines: deque = deque(maxlen=LINE_BUFFER_CAP)
        self._last_event_at: Optional[float] = None
        self._stale_timer = None
        self._workers: Optional[int] = None
        self._has_had_placement = False

    def compose(self) -> ComposeResult:
        yield Static(id="viz-progress")
        yield Static(id="viz-liveness")
        yield Static(id="viz-sparkline")
        yield Static(id="viz-grid", markup=True)
        yield Static("[bold]solver output[/bold]", id="viz-tail-label")
        yield Static(id="viz-tail", markup=True)
        yield Static(id="viz-footer")

    def on_mount(self) -> None:
        # The spinner frame is driven purely by `tick`; this timer only
        # re-renders the separate stale-detection text (wall-clock, since
        # "no signal" is inherently a real-time notion).
        self._stale_timer = self.set_interval(1.0, self._check_stale)

    def on_unmount(self) -> None:
        if self._stale_timer is not None:
            self._stale_timer.stop()
            self._stale_timer = None

    def handle_event(self, event_type: str, fields: dict) -> None:
        """Thread-safe entry point; called from the emit path."""
        try:
            self.app.call_from_thread(self._apply, event_type, fields)
        except Exception:
            pass

    def _check_stale(self) -> None:
        """Timer callback (main thread) — re-render only the liveness line."""
        try:
            self._render_liveness()
        except Exception:
            pass

    def _apply(self, event_type: str, fields: dict) -> None:
        """Update state and re-render. Must run on the main thread."""
        try:
            self._last_event_at = time.monotonic()
            if event_type == "viz_init":
                self._time_limit = fields.get("time_limit") or 0.0
                self._depts = list(fields.get("depts") or [])
                self._slots = list(fields.get("slots") or [])
                self._objective_series.clear()
                self._bound_series.clear()
                self._last_placed = []
                self._phase = "presolve"
                self._t = 0.0
                self._gap_pct = None
                self._solutions = None
                self._moved = None
                self._conflicts = None
                self._branches = None
                self._restarts = None
                self._quiet_for = None
                self._tick = None
                self._log_lines = None
                self._lines.clear()
                self._workers = None
                self._has_had_placement = False
                self.display = True
            elif event_type == "phase":
                self._phase = fields.get("phase", self._phase)
                self._t = fields.get("t", self._t)
                if fields.get("gap_pct") is not None:
                    self._gap_pct = fields["gap_pct"]
                if fields.get("workers") is not None:
                    self._workers = fields["workers"]
            elif event_type == "incumbent":
                self._t = fields.get("t", self._t)
                if fields.get("n") is not None:
                    self._solutions = fields["n"]
                if fields.get("objective") is not None:
                    self._objective_series.append(fields["objective"])
                if fields.get("bound") is not None:
                    self._bound_series.append(fields["bound"])
                if fields.get("gap_pct") is not None:
                    self._gap_pct = fields["gap_pct"]
                if fields.get("conflicts") is not None:
                    self._conflicts = fields["conflicts"]
                if fields.get("branches") is not None:
                    self._branches = fields["branches"]
                if fields.get("placed") is not None:
                    self._last_placed = fields["placed"]
                    self._has_had_placement = True
                if fields.get("moved") is not None:
                    self._moved = fields["moved"]
            elif event_type == "bound":
                self._t = fields.get("t", self._t)
                if fields.get("bound") is not None:
                    self._bound_series.append(fields["bound"])
                if fields.get("objective") is not None:
                    self._objective_series.append(fields["objective"])
                if fields.get("gap_pct") is not None:
                    self._gap_pct = fields["gap_pct"]
            elif event_type == "activity":
                self._t = fields.get("t", self._t)
                self._phase = fields.get("phase", self._phase)
                if fields.get("solutions") is not None:
                    self._solutions = fields["solutions"]
                self._quiet_for = fields.get("quiet_for", self._quiet_for)
                if fields.get("conflicts") is not None:
                    self._conflicts = fields["conflicts"]
                if fields.get("branches") is not None:
                    self._branches = fields["branches"]
                if fields.get("restarts") is not None:
                    self._restarts = fields["restarts"]
                if fields.get("gap_pct") is not None:
                    self._gap_pct = fields["gap_pct"]
                if fields.get("tick") is not None:
                    self._tick = fields["tick"]
                if fields.get("log_lines") is not None:
                    self._log_lines = fields["log_lines"]
                last_log = fields.get("last_log")
                if last_log and (not self._lines or self._lines[-1] != last_log):
                    self._lines.append(str(last_log))
            elif event_type == "solver_line":
                line = fields.get("line")
                if line:
                    self._lines.append(str(line))
                if fields.get("tick") is not None:
                    self._tick = fields["tick"]
                if fields.get("log_lines") is not None:
                    self._log_lines = fields["log_lines"]
                self._t = fields.get("t", self._t)
            else:
                return
            self._render_all()
        except Exception:
            pass

    def _render_all(self) -> None:
        try:
            self.query_one("#viz-progress", Static).update(
                format_progress_bar(
                    self._t, self._time_limit, self._phase, self._gap_pct,
                    workers=self._workers,
                )
            )
        except Exception:
            pass
        self._render_liveness()
        try:
            spark = render_sparkline(self._objective_series, self._bound_series)
            activity = format_activity_line(self._solutions, self._quiet_for)
            text = spark if not activity else f"{spark}\n{activity}"
            self.query_one("#viz-sparkline", Static).update(text)
        except Exception:
            pass
        try:
            placeholder = (
                None
                if self._has_had_placement
                else render_grid_placeholder(self._phase, self._workers)
            )
            self.query_one("#viz-grid", Static).update(
                render_grid(
                    self._slots, self._depts, self._last_placed,
                    placeholder=placeholder,
                )
            )
        except Exception:
            pass
        try:
            self.query_one("#viz-tail", Static).update(render_tail(self._lines))
        except Exception:
            pass
        try:
            self.query_one("#viz-footer", Static).update(
                format_footer(
                    solutions=self._solutions,
                    moved=self._moved,
                    conflicts=self._conflicts,
                    branches=self._branches,
                    restarts=self._restarts,
                    log_lines=self._log_lines,
                    elapsed=self._t,
                )
            )
        except Exception:
            pass

    def _render_liveness(self) -> None:
        try:
            since = (
                None
                if self._last_event_at is None
                else time.monotonic() - self._last_event_at
            )
            spinner = format_spinner(self._tick)
            stale = format_stale(since)
            text = spinner if not stale else f"{spinner}  [dim]{stale}[/dim]"
            self.query_one("#viz-liveness", Static).update(text)
        except Exception:
            pass
