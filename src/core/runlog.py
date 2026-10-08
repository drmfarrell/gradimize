"""FERPA-safe run logs — a debugging channel guaranteed free of student data.

Why this exists: once real (de-identified) extracts arrive, debugging happens
by reading these logs — including by an LLM assistant whose context leaves the
building. FERPA-protected student data (ids, grades, per-student attributes,
student↔course links) must therefore never be written here, even in
pseudonymized form. Course codes, room ids, faculty ids, time slots, counts,
and aggregates ARE allowed: catalog/directory-level data the institution
publishes anyway.

The guarantee is structural, not filter-based:

- Events are structured kwargs, not formatted prose, so a cell value can't be
  f-stringed in by accident. Student-file error sites log the value's *shape*
  (via ``describe()``), never the value.
- Exception messages embed cell contents (``int("X")`` puts X in the message),
  so ``exception()`` records type + stack frames and, on student-data paths,
  drops the message entirely. Elsewhere the message passes through
  ``scrub_text()``, which blanks short quoted tokens — a best-effort backstop,
  NOT the primary defense (the primary defense is that student values never
  reach those code paths).
- ``tests/test_runlog_canary.py`` plants sentinel student values through the
  full import + doctor path and asserts they never reach the log directory.
  Any new logging that violates the policy fails that test.

Writing must never break the host operation: every method swallows its own
exceptions, and a RunLog that fails to open degrades to a silent no-op. A
diagnostic feature must not be able to kill an import or a ten-minute solve.

Log location: ``$GRADIMIZE_LOG_DIR``, else ``<repo>/logs`` (gitignored).
Deliberately NOT under ``$GRADIMIZE_DATA_DIR``: the isolation rule is "data
never in the repo, logs never in the data zone" — logs are the one channel
the assistant may read, so they must not live behind the FERPA wall (first
real use hit exactly that trap). One JSONL file per run:
``run-YYYYMMDD-HHMMSS-<kind>.jsonl``.
"""

import json
import math
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Newest N run files kept per directory; older ones are pruned at open time.
KEEP_RUNS = 60

# Longest exception message retained after scrubbing.
_MAX_MSG = 300

# Quoted tokens with no spaces ('12.5', 'ZZ9SID001', '%H:%M') are how CPython
# and pandas embed offending values in exception messages; blank them. The
# no-space requirement keeps prose contractions intact ("doesn't ... can't"
# must not have the span between apostrophes eaten).
_QUOTED_TOKEN = re.compile(r"([\"'])([^\"' ]{1,80})\1")


def runlog_dir() -> Path:
    """Resolve the run-log directory (env-overridable; see module docstring).

    GRADIMIZE_DATA_DIR is intentionally ignored here — logs must stay
    readable OUTSIDE the FERPA data zone.
    """
    env = os.environ.get("GRADIMIZE_LOG_DIR")
    if env:
        return Path(env)
    return _REPO_ROOT / "logs"


def scrub_text(text: str, max_len: int = _MAX_MSG) -> str:
    """Blank short quoted tokens in exception/error prose.

    Best-effort backstop for messages on paths where student values should
    never appear anyway. Known limitation: a quoted value containing a space
    survives — which is why student-data paths drop messages entirely instead
    of relying on this.
    """
    scrubbed = _QUOTED_TOKEN.sub(lambda m: m.group(1) + "…" + m.group(1), str(text))
    return scrubbed[:max_len]


def describe(value) -> str:
    """Map a cell value to a shape string that carries format, not content.

    "13/45/2026" → "text(medium, digits+punct=/)" tells you the export uses
    slashed dates without revealing the date. For tiny values (grades like
    "A+") the punctuation characters are withheld — listing them would nearly
    reconstruct the value.
    """
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, float):
        return "missing" if math.isnan(value) else "float"
    if isinstance(value, int):
        return "int"

    s = str(value).strip()
    if not s:
        return "blank"
    n = len(s)
    bucket = "tiny" if n <= 2 else "short" if n <= 8 else "medium" if n <= 24 else "long"
    try:
        int(s)
        return f"int-string({bucket})"
    except ValueError:
        pass
    try:
        float(s)
        return f"float-string({bucket})"
    except ValueError:
        pass

    classes = []
    if any(c.isalpha() for c in s):
        classes.append("letters")
    if any(c.isdigit() for c in s):
        classes.append("digits")
    if any(c.isspace() for c in s):
        classes.append("spaces")
    punct = sorted({c for c in s if not c.isalnum() and not c.isspace()})
    desc = "+".join(classes) if classes else "other"
    if punct:
        # Character list only for values long enough that it can't
        # reconstruct them.
        desc += "+punct" + (("=" + "".join(punct[:5])) if n > 4 else "")
    return f"text({bucket}, {desc})"


def _frames(exc: BaseException, limit: int = 12) -> list[str]:
    """Repo-relative "file:line in func" frames — locations carry no data."""
    out = []
    try:
        for fr in traceback.extract_tb(exc.__traceback__)[-limit:]:
            fn = fr.filename
            try:
                fn = str(Path(fn).resolve().relative_to(_REPO_ROOT))
            except ValueError:
                fn = Path(fn).name
            out.append(f"{fn}:{fr.lineno} in {fr.name}")
    except Exception:
        pass
    return out


class RunLog:
    """One JSONL file per run. Every method is exception-proof by design."""

    def __init__(self, kind: str, directory: Path | str | None = None):
        self._f = None
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.path: Path | None = None
        try:
            base = Path(directory) if directory else runlog_dir()
            base.mkdir(parents=True, exist_ok=True)
            self._prune(base)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-2", "-3", "-4", "-5"):
                candidate = base / f"run-{stamp}{suffix}-{kind}.jsonl"
                try:
                    self._f = candidate.open("x", encoding="utf-8")
                    self.path = candidate
                    break
                except FileExistsError:
                    continue
            if self._f is not None:
                self.event(
                    "run_start",
                    kind=kind,
                    started=datetime.now().isoformat(timespec="seconds"),
                    pid=os.getpid(),
                )
        except Exception:
            self._f = None  # degrade to a no-op; never break the host run
            self.path = None

    @classmethod
    def disabled(cls) -> "RunLog":
        """A no-op instance, so call sites never need None-guards."""
        rl = cls.__new__(cls)
        rl._f = None
        rl._lock = threading.Lock()
        rl._t0 = time.monotonic()
        rl.path = None
        return rl

    @staticmethod
    def _prune(base: Path) -> None:
        try:
            runs = sorted(base.glob("run-*.jsonl"))
            for old in runs[: max(0, len(runs) - KEEP_RUNS + 1)]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def event(self, etype: str, **fields) -> None:
        """Write one structured event. Solver callbacks and heartbeat threads
        call this concurrently, hence the lock; flush-per-event so a crashed
        process still leaves a readable log."""
        if self._f is None:
            return
        try:
            record = {"t": round(time.monotonic() - self._t0, 3), "event": etype}
            record.update(fields)
            line = json.dumps(record, default=str, ensure_ascii=False)
            with self._lock:
                if self._f is not None:
                    self._f.write(line + "\n")
                    self._f.flush()
        except Exception:
            pass

    def exception(self, where: str, exc: BaseException, include_msg: bool = True) -> None:
        """Record an exception safely: type + frames always; message only when
        the caller vouches the path carries no student data (and even then it
        is scrubbed)."""
        try:
            fields: dict = {
                "where": where,
                "type": type(exc).__name__,
                "frames": _frames(exc),
            }
            if include_msg:
                fields["msg"] = scrub_text(str(exc))
            self.event("exception", **fields)
        except Exception:
            pass

    def close(self, status: str | None = None) -> None:
        if self._f is None:
            return
        try:
            self.event("run_end", status=status,
                       elapsed=round(time.monotonic() - self._t0, 3))
            with self._lock:
                self._f.close()
                self._f = None
        except Exception:
            self._f = None

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # Generic exit path can't vouch for the data on it: no message.
            self.exception("run", exc_val, include_msg=False)
        self.close(status="error" if exc_val is not None else "ok")
        return False
