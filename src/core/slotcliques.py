"""Time-point cliques of mutually overlapping time slots.

Two slots overlap when they share a day and their clock times intersect
(:func:`src.core.timeutil.slots_overlap`). The overlap graph is NOT made of
cliques: a Monday lab and a Wednesday lab both overlap an MWF lecture slot
yet never each other. That is exactly the trap the March 2026 room bug fell
into — a "slot group" ``sum <= 1`` over every neighbour of a slot wrongly
forbade the two labs from sharing a room — and the pairwise fix that
followed is sound but weak (one constraint per overlapping pair, and a
loose LP relaxation).

The sound AND tight formulation goes by time point. On one day, the slots
active at a single instant form a clique, and any two overlapping slots are
both active at the later one's start time. So::

    { slots on day d active at t  :  d a day, t the start of some slot on d }

covers every overlapping pair, contains no non-overlapping pair, and is as
tight as a clique cover gets. "Resource X is never double-booked" then reads
one constraint per clique C: ``sum(busy[slot] for slot in C) <= 1``.

Used by the engine's room and faculty no-overlap constraints
(``solver.no_overlap_formulation: cliques``) and by the exact student
sectioning pass (:mod:`src.core.sectioning`).
"""

from collections import defaultdict
from collections.abc import Iterable

from src.core.timeutil import time_to_minutes
from src.database.models import TimeSlot


def compute_slot_cliques(time_slots: Iterable[TimeSlot]) -> list[frozenset[int]]:
    """Return the time-point cliques of ``time_slots`` as frozensets of slot ids.

    Guarantees: every pair of overlapping slots shares at least one clique;
    every clique is pairwise overlapping; no clique is a strict subset of
    another; the list is sorted so results are deterministic. Degenerate
    slots (end <= start) overlap nothing and appear in no clique.
    """
    by_day: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for slot in time_slots:
        start = time_to_minutes(slot.start_time)
        end = time_to_minutes(slot.end_time)
        if end <= start:
            continue
        for day in slot.days:
            by_day[day].append((start, end, slot.id))

    cliques: set[frozenset[int]] = set()
    for entries in by_day.values():
        for t in sorted({start for start, _end, _sid in entries}):
            active = frozenset(sid for start, end, sid in entries if start <= t < end)
            if active:
                cliques.add(active)

    # Drop strict subsets: the clique at one time point is often contained in
    # the clique at a later point on the same day.
    by_size = sorted(cliques, key=lambda c: (-len(c), sorted(c)))
    maximal: list[frozenset[int]] = []
    for clique in by_size:
        if not any(clique < bigger for bigger in maximal):
            maximal.append(clique)
    return sorted(maximal, key=lambda c: sorted(c))


def cliques_by_slot(cliques: list[frozenset[int]]) -> dict[int, list[int]]:
    """Index: slot id -> indices (into ``cliques``) of the cliques containing it."""
    index: dict[int, list[int]] = defaultdict(list)
    for i, clique in enumerate(cliques):
        for slot_id in clique:
            index[slot_id].append(i)
    return dict(index)
