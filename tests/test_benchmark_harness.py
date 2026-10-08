"""Tests for the pure pieces of scripts/benchmark_solver.py."""

import importlib.util
from pathlib import Path

from src.core.engine import SchedulerConfig

_spec = importlib.util.spec_from_file_location(
    "benchmark_solver", Path(__file__).parent.parent / "scripts" / "benchmark_solver.py"
)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_parse_override_types():
    assert bench.parse_override("solver.x=2") == (["solver", "x"], 2)
    assert bench.parse_override("hard.flag=true") == (["hard", "flag"], True)
    assert bench.parse_override("solver.mode=fast") == (["solver", "mode"], "fast")
    assert bench.parse_override("soft.a.b.weight=-30") == (["soft", "a", "b", "weight"], -30)


def test_apply_overrides_nested():
    cfg = SchedulerConfig()
    bench.apply_overrides(cfg, [
        bench.parse_override("solver.linearization_level=2"),
        bench.parse_override("soft.new_pen.faculty.weight=-30"),
        bench.parse_override("hard.some_flag=false"),
        bench.parse_override("section.thing=5"),
    ])
    assert cfg.solver_settings["linearization_level"] == 2
    assert cfg.soft_constraints["new_pen"]["faculty"]["weight"] == -30
    assert cfg.hard_constraints["some_flag"] is False
    assert cfg.section_settings["thing"] == 5


def test_write_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "r.jsonl"
    bench.write_record(p, {"label": "a", "x": 1})
    bench.write_record(p, {"label": "b", "x": None})
    assert bench.load_records(p) == [{"label": "a", "x": 1}, {"label": "b", "x": None}]


def test_render_markdown():
    md = bench.render_markdown([
        {"timestamp": "2026-10-07T12:34:56", "label": "foo-baseline", "status": "OPTIMAL",
         "room_conflicts": 0, "faculty_conflicts": 0},
        {"timestamp": "2026-10-07T13:00:00", "label": "bar", "gap_pct": None},
    ])
    assert "foo-baseline" in md and "bar" in md
    assert "2026-10-07 12:34" in md
    assert "| - |" in md
