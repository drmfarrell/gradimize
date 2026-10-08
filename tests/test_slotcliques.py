"""Tests for src.core.slotcliques: sound and complete time-point cliques."""

import random
from itertools import combinations

from src.core.slotcliques import cliques_by_slot, compute_slot_cliques
from src.core.timeutil import slots_overlap
from src.database.models import TimeSlot


def _slot(sid, days, start, end, dur=None):
    return TimeSlot(id=sid, name=f"S{sid}", pattern="".join(days), days=list(days),
                    start_time=start, end_time=end, duration_minutes=dur or 0,
                    slot_type="X")


def _hm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _random_slots():
    rng = random.Random(0)
    patterns = [["M"], ["T"], ["W"], ["R"], ["F"], ["M", "W", "F"], ["T", "R"], ["M", "W"]]
    slots = []
    for sid in range(1, 41):
        start = rng.randrange(8 * 60, 18 * 60, 15)
        dur = rng.randrange(30, 181, 5)
        slots.append(_slot(sid, rng.choice(patterns), _hm(start), _hm(start + dur), dur))
    return slots


def test_march_2026_shape():
    slots = [
        _slot(1, "MWF", "08:00", "08:50", 50),
        _slot(19, "M", "08:00", "10:30", 150),
        _slot(25, "W", "08:00", "10:30", 150),
    ]
    cliques = compute_slot_cliques(slots)
    assert any({1, 19} <= c for c in cliques)
    assert any({1, 25} <= c for c in cliques)
    assert not any({19, 25} <= c for c in cliques)


def test_sound_and_complete_on_random_slots():
    slots = _random_slots()
    cliques = compute_slot_cliques(slots)
    for a, b in combinations(slots, 2):
        together = any(a.id in c and b.id in c for c in cliques)
        assert together == slots_overlap(a, b), (a.id, b.id)
    by_id = {s.id: s for s in slots}
    for c in cliques:
        for x, y in combinations(sorted(c), 2):
            assert slots_overlap(by_id[x], by_id[y])


def test_maximal_deterministic_and_degenerate_excluded():
    slots = _random_slots() + [_slot(99, "MWF", "10:00", "10:00", 0)]
    cliques = compute_slot_cliques(slots)
    assert not any(a < b for a in cliques for b in cliques)
    assert cliques == compute_slot_cliques(slots)
    assert cliques == compute_slot_cliques(list(reversed(slots)))
    assert not any(99 in c for c in cliques)


def test_cliques_by_slot_index():
    cliques = compute_slot_cliques(_random_slots())
    index = cliques_by_slot(cliques)
    for sid, idxs in index.items():
        assert idxs == [i for i, c in enumerate(cliques) if sid in c]
    for i, c in enumerate(cliques):
        for sid in c:
            assert i in index[sid]
