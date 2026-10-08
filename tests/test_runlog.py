"""Unit tests for the FERPA-safe run log (src/core/runlog.py).

The invariant under test: nothing that goes INTO a RunLog can carry a raw
cell value out — shapes, scrubbed messages, frames, and counts only. The
end-to-end enforcement lives in tests/test_runlog_canary.py; these tests pin
the building blocks.
"""

import json
import threading
from pathlib import Path

import pytest

from src.core.runlog import (
    KEEP_RUNS,
    RunLog,
    describe,
    runlog_dir,
    scrub_text,
)


def read_events(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


# ─────────────────────────────────────────────────────────────────────
# describe() — shapes carry format, never content
# ─────────────────────────────────────────────────────────────────────

class TestDescribe:
    @pytest.mark.parametrize("value,expected", [
        (None, "missing"),
        (float("nan"), "missing"),
        (True, "bool"),
        (7, "int"),
        (3.5, "float"),
        ("", "blank"),
        ("   ", "blank"),
        ("12", "int-string(tiny)"),
        ("117", "int-string(short)"),
        ("12.5", "float-string(short)"),
        ("BIO101", "text(short, letters+digits)"),
    ])
    def test_scalars(self, value, expected):
        assert describe(value) == expected

    def test_date_like_reveals_format_not_value(self):
        # The debugging payload: "the export uses slashed dates".
        assert describe("13/45/2026") == "text(medium, digits+punct=/)"

    def test_tiny_values_hide_punct_chars(self):
        # For a 2-char value, listing the punctuation ("+") would nearly
        # reconstruct it (grade "A+"). Presence only.
        assert describe("A+") == "text(tiny, letters+punct)"

    def test_long_bucket(self):
        assert describe("x" * 30).startswith("text(long")

    @pytest.mark.parametrize("value", [
        "ZZ9SID001", "Jane Q Student", "A+", "13/45/2026", "WF",
    ])
    def test_shape_never_contains_the_value(self, value):
        shape = describe(value)
        assert value not in shape
        # No fragment of 3+ consecutive characters survives either.
        for i in range(len(value) - 2):
            assert value[i:i + 3] not in shape


# ─────────────────────────────────────────────────────────────────────
# scrub_text() — quoted-token backstop
# ─────────────────────────────────────────────────────────────────────

class TestScrubText:
    def test_int_parse_message_scrubbed(self):
        # The classic leak: int("X") embeds X in the exception message.
        try:
            int("ZZ9CRED1")
        except ValueError as e:
            out = scrub_text(str(e))
        assert "ZZ9CRED1" not in out
        assert "invalid literal" in out  # the diagnostic template survives

    def test_keyerror_scrubbed(self):
        try:
            {}["ZZ9SID001"]
        except KeyError as e:
            assert "ZZ9SID001" not in scrub_text(str(e))

    def test_double_quotes(self):
        assert "ZZ9TOKEN" not in scrub_text('bad value "ZZ9TOKEN" in row')

    def test_contractions_survive(self):
        # The no-space rule exists exactly so apostrophe prose isn't eaten.
        msg = "the solver doesn't converge and can't prove optimality"
        assert scrub_text(msg) == msg

    def test_truncation(self):
        assert len(scrub_text("x" * 1000)) == 300


# ─────────────────────────────────────────────────────────────────────
# RunLog — lifecycle, robustness, threading
# ─────────────────────────────────────────────────────────────────────

class TestRunLog:
    def test_writes_jsonl_with_elapsed(self, tmp_path):
        rl = RunLog("test", directory=tmp_path)
        rl.event("hello", a=1, b="x")
        rl.close(status="ok")
        events = read_events(rl.path)
        assert [e["event"] for e in events] == ["run_start", "hello", "run_end"]
        assert events[1]["a"] == 1
        assert all("t" in e for e in events)
        assert events[-1]["status"] == "ok"

    def test_env_var_overrides_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GRADIMIZE_LOG_DIR", str(tmp_path / "envlogs"))
        assert runlog_dir() == tmp_path / "envlogs"
        rl = RunLog("test")
        rl.close()
        assert rl.path.parent == tmp_path / "envlogs"

    def test_data_dir_is_ignored(self, monkeypatch, tmp_path):
        # Logs must never resolve into the FERPA data zone: with only
        # GRADIMIZE_DATA_DIR set, logs still go to the repo's logs/.
        monkeypatch.delenv("GRADIMIZE_LOG_DIR", raising=False)
        monkeypatch.setenv("GRADIMIZE_DATA_DIR", str(tmp_path / "data"))
        resolved = runlog_dir()
        assert resolved.name == "logs"
        assert not str(resolved).startswith(str(tmp_path / "data"))

    def test_disabled_is_total_noop(self):
        rl = RunLog.disabled()
        rl.event("x", a=1)
        rl.exception("w", ValueError("boom"))
        rl.close()
        assert rl.path is None

    def test_unopenable_dir_degrades_silently(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("a file where the dir should be")
        rl = RunLog("test", directory=blocker)  # mkdir fails
        rl.event("x")  # must not raise
        rl.close()
        assert rl.path is None

    def test_event_after_close_is_noop(self, tmp_path):
        rl = RunLog("test", directory=tmp_path)
        rl.close()
        rl.event("late")  # must not raise
        assert "late" not in rl.path.read_text()

    def test_nonserializable_fields_stringified(self, tmp_path):
        rl = RunLog("test", directory=tmp_path)
        rl.event("odd", obj=object())  # json default=str; must not raise
        rl.close()
        assert any(e["event"] == "odd" for e in read_events(rl.path))

    def test_same_second_runs_get_distinct_files(self, tmp_path):
        a = RunLog("kind", directory=tmp_path)
        b = RunLog("kind", directory=tmp_path)
        assert a.path != b.path
        a.close()
        b.close()

    def test_retention_prunes_oldest(self, tmp_path):
        for i in range(KEEP_RUNS + 10):
            (tmp_path / f"run-20200101-{i:06d}-old.jsonl").write_text("{}")
        rl = RunLog("new", directory=tmp_path)
        rl.close()
        remaining = {p.name for p in tmp_path.glob("run-*.jsonl")}
        assert len(remaining) <= KEEP_RUNS
        assert rl.path.name in remaining
        assert "run-20200101-000000-old.jsonl" not in remaining

    def test_exception_frames_without_message(self, tmp_path):
        rl = RunLog("test", directory=tmp_path)
        try:
            int("ZZ9CRED1")
        except ValueError as e:
            rl.exception("import", e, include_msg=False)
        rl.close()
        assert "ZZ9CRED1" not in rl.path.read_text()
        ev = next(e for e in read_events(rl.path) if e["event"] == "exception")
        assert ev["type"] == "ValueError"
        assert ev["frames"] and "test_runlog.py" in ev["frames"][-1]
        assert "msg" not in ev

    def test_exception_message_scrubbed_when_included(self, tmp_path):
        rl = RunLog("test", directory=tmp_path)
        try:
            int("ZZ9CRED1")
        except ValueError as e:
            rl.exception("solve", e, include_msg=True)
        rl.close()
        text = rl.path.read_text()
        assert "ZZ9CRED1" not in text
        assert "invalid literal" in text

    def test_context_manager_drops_exception_message(self, tmp_path):
        # __exit__ can't vouch for what's on the path → no message at all.
        with pytest.raises(ValueError):
            with RunLog("test", directory=tmp_path) as rl:
                raise ValueError("boom ZZ9SECRET unquoted")
        text = rl.path.read_text()
        assert "ZZ9SECRET" not in text
        events = read_events(rl.path)
        assert events[-1]["event"] == "run_end"
        assert events[-1]["status"] == "error"

    def test_concurrent_events_from_threads(self, tmp_path):
        # Solver callback + heartbeat threads write concurrently in real use.
        rl = RunLog("test", directory=tmp_path)

        def spam(worker: int):
            for i in range(100):
                rl.event("spam", worker=worker, i=i)

        threads = [threading.Thread(target=spam, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rl.close()
        events = read_events(rl.path)  # json.loads raises on torn lines
        assert sum(1 for e in events if e["event"] == "spam") == 400
