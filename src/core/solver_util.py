"""Shared CP-SAT solver helpers."""

import os

# Cores to leave free for the API/web server so it stays responsive while a
# solve saturates the machine. On a 64-core box this caps auto solves to 60.
RESERVED_CORES = 4


def resolve_worker_count(requested: int = 0) -> int:
    """Resolve how many CP-SAT search workers to use.

    A positive `requested` is honored as-is (explicit override). `0` means
    "auto", which we cap to `cpu_count - RESERVED_CORES` (min 1) rather than
    letting CP-SAT grab every core — that headroom keeps the web server from
    lagging while a solve runs.
    """
    if requested and requested > 0:
        return requested
    cores = os.cpu_count() or 8
    return max(1, cores - RESERVED_CORES)
