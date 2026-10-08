"""Unit tests for the pure rendering helpers in src.tui.widgets.solver_viz.

These test string-in/string-out functions only; no Textual app is
instantiated.
"""

from src.tui.widgets.solver_viz import (
    PALETTE,
    SPINNER_FRAMES,
    format_activity_line,
    format_footer,
    format_progress_bar,
    format_spinner,
    format_stale,
    render_grid,
    render_grid_placeholder,
    render_sparkline,
    render_tail,
)

# ── progress bar ─────────────────────────────────────────────────────────


def test_progress_bar_basic():
    line = format_progress_bar(t=30, time_limit=60, phase="improving", gap_pct=2.4)
    assert "Improving" in line
    assert "0:30 / 1:00" in line
    assert "gap 2.4%" in line


def test_progress_bar_clamps_negative_to_zero_fill():
    line = format_progress_bar(t=-5, time_limit=100, phase="presolve")
    # No filled blocks at all.
    assert "█" not in line.split("[")[1].split("]")[0]


def test_progress_bar_clamps_over_100_percent():
    full = format_progress_bar(t=100, time_limit=100, phase="done", width=10)
    over = format_progress_bar(t=1000, time_limit=100, phase="done", width=10)
    bar_full = full.split("[")[1].split("]")[0]
    bar_over = over.split("[")[1].split("]")[0]
    assert bar_full == bar_over == "█" * 10


def test_progress_bar_omits_gap_when_unknown():
    line = format_progress_bar(t=10, time_limit=60, phase="presolve")
    assert "gap" not in line


def test_progress_bar_unknown_time_limit():
    # time_limit=0 must not raise a ZeroDivisionError.
    line = format_progress_bar(t=5, time_limit=0, phase="presolve")
    assert "0:00" in line


def test_progress_bar_searching_with_workers():
    line = format_progress_bar(t=10, time_limit=60, phase="searching", workers=60)
    assert "Searching · 60 workers" in line


def test_progress_bar_searching_without_workers():
    line = format_progress_bar(t=10, time_limit=60, phase="searching")
    assert "Searching" in line
    assert "workers" not in line


def test_progress_bar_other_phases_ignore_workers():
    line = format_progress_bar(t=10, time_limit=60, phase="presolve", workers=60)
    assert "workers" not in line


# ── sparkline ────────────────────────────────────────────────────────────


def test_sparkline_empty():
    assert render_sparkline([]) == "(no data yet)"


def test_sparkline_single_point():
    line = render_sparkline([42.0])
    assert line.startswith("obj  ")
    assert len(line.splitlines()[0]) == len("obj  ") + 1


def test_sparkline_many_points_truncates_to_width():
    values = list(range(200))
    line = render_sparkline(values, width=60)
    obj_line = line.splitlines()[0]
    assert len(obj_line) == len("obj  ") + 60


def test_sparkline_includes_bound_line_when_present():
    line = render_sparkline([1, 2, 3], bound_series=[4, 5, 6])
    lines = line.splitlines()
    assert lines[0].startswith("obj  ")
    assert lines[1].startswith("bnd  ")


def test_sparkline_no_bound_line_when_absent():
    line = render_sparkline([1, 2, 3])
    assert len(line.splitlines()) == 1


# ── activity line ────────────────────────────────────────────────────────


def test_activity_line_with_quiet_for():
    line = format_activity_line(5, 38.0)
    assert line == "5 solutions · last improved 0:38 ago"


def test_activity_line_without_quiet_for():
    assert format_activity_line(5, None) == "5 solutions"


def test_activity_line_no_solutions_yet():
    assert format_activity_line(None, None) == ""


# ── grid ─────────────────────────────────────────────────────────────────

SLOTS = [
    {"id": 1, "name": "MWF 8am", "pattern": "MWF", "days": ["M", "W", "F"],
     "start": "08:00", "end": "08:50"},
    {"id": 2, "name": "TR 9am", "pattern": "TR", "days": ["T", "R"],
     "start": "09:00", "end": "10:15"},
]


def test_grid_slot_spans_multiple_days():
    placed = [[1, 0, "BIO-101-001"]]
    grid = render_grid(SLOTS, ["BIO"], placed, cell_width=8)
    rows = grid.splitlines()
    row1 = next(r for r in rows if r.startswith("08:00"))
    # The MWF slot (id=1) should place one block in each of M, W, F and none
    # in T or R — three blocks total on this row.
    assert row1.count("█") == 3
    row2 = next(r for r in rows if r.startswith("09:00"))
    assert row2.count("█") == 0


def test_grid_dedupes_rows_by_start_end():
    slots = SLOTS + [
        {"id": 3, "name": "MWF 8am dup", "pattern": "MWF", "days": ["M"],
         "start": "08:00", "end": "08:50"},
    ]
    grid = render_grid(slots, [], [])
    header_and_rows = grid.splitlines()
    time_rows = [r for r in header_and_rows if r.startswith("08:00")]
    assert len(time_rows) == 1


def test_grid_cell_overflow_shows_plus_n():
    placed = [[1, i % 12, f"C-{i}"] for i in range(10)]
    grid = render_grid(SLOTS, [f"D{i}" for i in range(12)], placed, cell_width=4)
    assert "+" in grid


def test_grid_legend_uses_full_palette_with_wraparound():
    depts = [f"D{i}" for i in range(13)]
    grid = render_grid(SLOTS, depts, [], cell_width=4)
    # dept index 12 wraps back to PALETTE[0].
    assert PALETTE[0] in grid
    assert grid.count(PALETTE[0]) >= 2  # legend entries for D0 and D12


def test_grid_empty_placements_no_blocks():
    grid = render_grid(SLOTS, ["BIO"], [])
    rows = grid.splitlines()
    row1 = next(r for r in rows if r.startswith("08:00"))
    assert "█" not in row1


# ── grid placeholder (dead-space before the first schedule) ────────────────


def test_grid_placeholder_presolve():
    assert render_grid_placeholder("presolve") == (
        "Simplifying the problem — no schedule to show yet"
    )


def test_grid_placeholder_searching_without_workers():
    assert render_grid_placeholder("searching") == (
        "Searching for a first workable schedule"
    )


def test_grid_placeholder_searching_with_workers():
    assert render_grid_placeholder("searching", workers=60) == (
        "Searching for a first workable schedule (60 workers)"
    )


def test_grid_placeholder_other_phase():
    assert render_grid_placeholder("improving") == "Waiting for the first schedule"
    assert render_grid_placeholder("certifying") == "Waiting for the first schedule"


def test_grid_with_placeholder_keeps_header_and_row_count():
    normal = render_grid(SLOTS, ["BIO"], [])
    placeholder_text = render_grid_placeholder("presolve")
    placeheld = render_grid(SLOTS, ["BIO"], [], placeholder=placeholder_text)
    assert normal.splitlines()[0] == placeheld.splitlines()[0]  # same header
    # Same number of time rows (header + N slot rows + legend).
    assert len(normal.splitlines()) == len(placeheld.splitlines())
    assert placeholder_text in placeheld
    body = "\n".join(placeheld.splitlines()[1:-1])  # exclude header and legend
    assert "█" not in body


def test_grid_placeholder_disappears_once_placed_is_given():
    grid = render_grid(SLOTS, ["BIO"], [[1, 0, "BIO-101-001"]], placeholder=None)
    assert "Waiting for the first schedule" not in grid


# ── footer ───────────────────────────────────────────────────────────────


def test_footer_omits_none_fields():
    line = format_footer(solutions=5, elapsed=65)
    assert line == "solutions 5 · elapsed 1:05"
    assert "None" not in line
    assert "conflicts" not in line
    assert "moved" not in line
    assert "branches" not in line
    assert "restarts" not in line


def test_footer_all_fields_present():
    line = format_footer(
        solutions=3, moved=2, conflicts=7, branches=100, restarts=1, elapsed=5
    )
    assert line == "solutions 3 · moved 2 · conflicts 7 · branches 100 · restarts 1 · elapsed 0:05"


def test_footer_all_none_is_empty():
    assert format_footer() == ""


def test_footer_log_lines_formatted_with_commas():
    line = format_footer(log_lines=1284)
    assert line == "output 1,284 lines"


def test_footer_log_lines_omitted_when_none():
    line = format_footer(solutions=1)
    assert "output" not in line


# ── spinner ──────────────────────────────────────────────────────────────


def test_spinner_frame_from_tick():
    for tick, expected in enumerate(SPINNER_FRAMES):
        assert format_spinner(tick) == expected


def test_spinner_wraps_past_ten():
    assert format_spinner(10) == SPINNER_FRAMES[0]
    assert format_spinner(13) == SPINNER_FRAMES[3]
    assert format_spinner(23) == SPINNER_FRAMES[3]


def test_spinner_no_tick_yet():
    assert format_spinner(None) not in SPINNER_FRAMES


# ── stale detection ──────────────────────────────────────────────────────


def test_stale_within_threshold_is_empty():
    assert format_stale(4.9) == ""


def test_stale_unknown_is_empty():
    assert format_stale(None) == ""


def test_stale_past_threshold():
    assert format_stale(7.2) == "no signal for 7s"


def test_stale_custom_threshold():
    assert format_stale(3.0, threshold=2.0) == "no signal for 3s"
    assert format_stale(1.0, threshold=2.0) == ""


# ── machine-room tail ────────────────────────────────────────────────────


def test_tail_empty():
    assert render_tail([]) == "(no output yet)"


def test_tail_fewer_lines_than_window():
    lines = ["first", "second"]
    tail = render_tail(lines, width=80, max_lines=4)
    rows = tail.splitlines()
    assert len(rows) == 2
    # Oldest dimmed, newest (bottom) not.
    assert rows[0] == "[dim]first[/dim]"
    assert rows[1] == "second"


def test_tail_overflow_keeps_last_n_newest_at_bottom():
    lines = [f"line{i}" for i in range(10)]
    tail = render_tail(lines, width=80, max_lines=4)
    rows = tail.splitlines()
    assert len(rows) == 4
    assert rows[-1] == "line9"
    assert "line6" in rows[0]
    assert "line0" not in tail


def test_tail_truncates_to_width_without_wrapping():
    long_line = "x" * 200
    tail = render_tail([long_line], width=20, max_lines=4)
    # Single line, no markup (it is the newest/only line), truncated.
    assert tail == "x" * 20
    assert "\n" not in tail
