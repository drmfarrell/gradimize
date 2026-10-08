"""Process-wide single-optimization guard.

The schedule generator, re-solver, and finals solver are CPU- and memory-heavy
CP-SAT runs over a single SQLite database. Running two at once contends for
resources and can wedge the process, and the app is intentionally single-user
for now. So we allow only ONE optimization at a time, process-wide.

This is a deliberately simple in-memory guard, NOT a distributed lock: it
assumes a single server process (one uvicorn worker, which is how the app is
deployed). Acquire before starting any solve and release in a finally; the
current status is exposed so the web UI can disable the optimize controls
while a run is in progress.
"""

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

_lock = threading.Lock()
_current: Optional[dict] = None

# Canonical 409 detail shown when the slot is already held. One wording, so the
# UI sees the same message from every guarded endpoint (generate, re-solve,
# repair, rebalance, import).
BUSY_MESSAGE = (
    "An optimization is already running. Please wait for it to finish before "
    "starting another."
)


class BusyError(RuntimeError):
    """Raised by :func:`hold` when another optimization already owns the slot.

    Carries the canonical :data:`BUSY_MESSAGE`; route handlers translate it to
    a 409.
    """

    def __init__(self) -> None:
        super().__init__(BUSY_MESSAGE)


@contextmanager
def hold(kind: str, label: str):
    """Hold the optimization slot for the duration of a ``with`` block.

    Acquires on entry (raising :class:`BusyError` if one is already running)
    and releases on exit, so callers cannot forget the ``finally``. Prefer this
    over bare try_acquire/release at new call sites.
    """
    if not try_acquire(kind, label):
        raise BusyError()
    try:
        yield
    finally:
        release()


def try_acquire(kind: str, label: str) -> bool:
    """Claim the optimization slot. Returns False if one is already running.

    Args:
        kind: Short machine tag — "generate", "re-solve", or "finals".
        label: Human-readable name (e.g. the schedule name) for the UI.
    """
    global _current
    with _lock:
        if _current is not None:
            return False
        _current = {
            "kind": kind,
            "label": label,
            "started_at": datetime.now().isoformat(),
        }
        return True


def release() -> None:
    """Release the optimization slot. Safe to call even if not held."""
    global _current
    with _lock:
        _current = None


def status() -> dict:
    """Current optimization status for the UI. `{running: bool, ...}`."""
    with _lock:
        if _current is None:
            return {"running": False}
        return {"running": True, **_current}
