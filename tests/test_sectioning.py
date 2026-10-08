"""Tests for src.core.sectioning (exact student sectioning, phase B)."""

from dataclasses import dataclass

import pytest

from src.core.sectioning import (
    SectionSpec,
    count_student_overlaps,
    greedy_seats,
    section_students,
)
from src.core.slotcliques import compute_slot_cliques
from src.database.models import TimeSlot


@dataclass
class D:
    student_id: str
    course_code: str
    urgency_score: float = 10.0
    is_graduation_critical: bool = False


def _slot(sid, days, start, end):
    return TimeSlot(id=sid, name=f"S{sid}", pattern="".join(days), days=list(days),
                    start_time=start, end_time=end, duration_minutes=0, slot_type="X")


CLIQUES = compute_slot_cliques([
    _slot(1, "MWF", "08:00", "08:50"),
    _slot(2, "MWF", "09:00", "09:50"),
    _slot(3, "TR", "08:00", "09:15"),
    _slot(4, "M", "08:00", "10:30"),
])


def solve(sections, demands, **kw):
    return section_students(sections, demands, CLIQUES, num_workers=2,
                            time_limit_seconds=10, **kw)


def test_overlap_prevented():
    secs = [SectionSpec("a1", "A", 5, (1,)), SectionSpec("b1", "B", 5, (1,))]
    r = solve(secs, [D("S", "A", 10), D("S", "B", 20)])
    assert r.seats == {("S", "b1")}
    assert count_student_overlaps({("S", "a1"), ("S", "b1")}, secs, CLIQUES) == 1
    assert count_student_overlaps(r.seats, secs, CLIQUES) == 0


def test_non_overlapping_both_seated():
    secs = [SectionSpec("a1", "A", 5, (1,)), SectionSpec("b1", "B", 5, (2,))]
    r = solve(secs, [D("S", "A", 10), D("S", "B", 20)])
    assert r.seats == {("S", "a1"), ("S", "b1")}


def test_multi_meeting_collision():
    secs = [SectionSpec("a1", "A", 5, (1,)), SectionSpec("b1", "B", 5, (3, 4))]
    r = solve(secs, [D("S", "A", 10), D("S", "B", 20)])
    assert r.seats == {("S", "b1")}


def test_capacity():
    secs = [SectionSpec("a1", "A", 2, (1,))]
    r = solve(secs, [D("S1", "A", 5), D("S2", "A", 9), D("S3", "A", 7)])
    assert r.seats == {("S2", "a1"), ("S3", "a1")}


def test_single_enrollment_per_course():
    secs = [SectionSpec("a1", "A", 5, (1,)), SectionSpec("a2", "A", 5, (2,))]
    r = solve(secs, [D("S", "A")])
    assert len(r.seats) == 1


def test_crosslist_pack_capacity():
    secs = [SectionSpec("h1", "HIST", 3, (1,), "XL:001"),
            SectionSpec("f1", "AFAM", 3, (1,), "XL:001")]
    demands = [D(f"H{i}", "HIST") for i in range(3)] + [D(f"F{i}", "AFAM") for i in range(3)]
    r = solve(secs, demands)
    assert len(r.seats) == 3


def test_graduation_critical_hard():
    secs = [SectionSpec("a1", "A", 1, (1,))]
    r = solve(secs, [D("lo", "A", 1, True), D("hi", "A", 50)])
    assert r.seats == {("lo", "a1")}


def test_graduation_critical_fallback_and_off():
    secs = [SectionSpec("a1", "A", 1, (1,))]
    demands = [D("c1", "A", 5, True), D("c2", "A", 6, True)]
    r = solve(secs, demands)
    assert r.ok and r.graduation_critical_relaxed
    assert len(r.unmet_graduation_critical) == 1
    r = solve(secs, demands, graduation_critical="off")
    assert not r.graduation_critical_relaxed
    assert r.unmet_graduation_critical == []


def test_hint_accepted():
    secs = [SectionSpec("a1", "A", 1, (1,)), SectionSpec("b1", "B", 5, (2,))]
    demands = [D("S1", "A", 5), D("S2", "A", 9), D("S1", "B", 3)]
    known = {("S2", "a1"), ("S1", "b1")}
    r = solve(secs, demands, hint=known)
    assert r.seats == known


def test_seat_anchor():
    secs = [SectionSpec("500", "A", 1, (1,))]
    r = solve(secs, [D("S1", "A", 10), D("S2", "A", 10)],
              seat_anchor={("S2", "500"): 500})
    assert r.seats == {("S2", "500")}


def test_empty_inputs():
    r = solve([], [])
    assert r.status == "optimal" and not r.seats


# ── 2026-10-08: exact graduation-critical seating, fixed seats, LP rounding ──


def _crit_instance():
    """S1 (urgency 30) is graduation-critical for A and B; S2 (urgency 10) for A.

    A has sections a1 (slot 1) and a2 (slot 2), B has b1 (slot 1), all
    capacity 1. The only full seating is S1->a2+b1, S2->a1. A greedy that
    walks S1 first takes a1 (first CRN) and then cannot seat S1's B; the
    exact critical stage must find the full seating instead.
    """
    from src.core.slotcliques import compute_slot_cliques
    from src.database.models import TimeSlot

    slots = [
        TimeSlot(id=1, name="MWF_0800", pattern="MWF", days=["M", "W", "F"],
                 start_time="08:00", end_time="08:50", duration_minutes=50, slot_type="MWF_50"),
        TimeSlot(id=2, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
                 start_time="09:00", end_time="09:50", duration_minutes=50, slot_type="MWF_50"),
    ]
    sections = [
        SectionSpec("a1", "A", 1, (1,)),
        SectionSpec("a2", "A", 1, (2,)),
        SectionSpec("b1", "B", 1, (1,)),
    ]
    demands = [
        D("S1", "A", 30.0, True),
        D("S1", "B", 30.0, True),
        D("S2", "A", 10.0, True),
    ]
    return sections, demands, compute_slot_cliques(slots)


def test_greedy_alone_strands_a_critical_student():
    sections, demands, cliques = _crit_instance()
    seats = greedy_seats(sections, demands, cliques)
    seated_courses = {(sid, next(s.course_code for s in sections if s.crn == crn))
                      for sid, crn in seats}
    assert ("S1", "B") not in seated_courses  # the trap this stage exists for


def test_greedy_fixed_seats_are_taken_first():
    sections, demands, cliques = _crit_instance()
    seats = greedy_seats(sections, demands, cliques, fixed=[("S1", "a2"), ("S2", "a1")])
    assert seats == {("S1", "a2"), ("S1", "b1"), ("S2", "a1")}


def test_exact_critical_stage_seats_everyone():
    sections, demands, cliques = _crit_instance()
    result = section_students(sections, demands, cliques, num_workers=2, time_limit_seconds=0)
    assert result.ok
    assert result.unmet_graduation_critical == []
    assert result.graduation_critical_relaxed is False
    assert result.seats == {("S1", "a2"), ("S1", "b1"), ("S2", "a1")}


def test_lp_stage_reports_bound_and_gap():
    pytest.importorskip("ortools.math_opt.python.mathopt")
    sections, demands, cliques = _crit_instance()
    result = section_students(sections, demands, cliques, num_workers=2, time_limit_seconds=0)
    assert result.lp_bound is not None
    assert result.gap_pct is not None and result.gap_pct >= 0.0
    # Three seats at 30, 30 and 10 urgency: the LP bound equals the optimum.
    assert result.lp_bound == pytest.approx(7000.0)
    assert result.gap_pct == pytest.approx(0.0)
    assert result.method in ("greedy", "lp-round")


def test_lp_stage_can_be_disabled():
    sections, demands, cliques = _crit_instance()
    result = section_students(
        sections, demands, cliques, num_workers=2, time_limit_seconds=0, use_lp=False,
    )
    assert result.ok and result.lp_bound is None and result.gap_pct is None
