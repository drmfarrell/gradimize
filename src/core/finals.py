"""Finals exam scheduling engine.

CP-SAT solver that assigns each section needing a final to a finals exam
block, minimizing student conflicts and time-of-day mismatch with the
regular meeting. Rooms are reused from the regular semester schedule.

Hard constraints
- Each section needing a final is assigned exactly one block (unless pinned
  in-place per the institution's finals policy for late/evening classes).
- Two sections sharing a room cannot share a block.

Soft objective (minimized)
- Student block conflicts (two exams in one block) and overlap with an
  in-place section sharing students — heavily weighted penalties, not hard
  constraints, so a schedule always exists; residual conflicts are reported
  in solver_stats for resolution on the conflict day.
- Time-of-day mismatch between class start and block start (per minute).
- Day-of-week mismatch (MWF classes prefer M/W; TR prefer T/R).
- Overflow-day usage (Friday by default).
- Per-student per-day "two exams same day" and "three+ exams same day".
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ortools.sat.python import cp_model
from sqlmodel import Session, select

from src.core.solver_util import resolve_worker_count
from src.core.timeutil import time_to_minutes
from src.database.models import (
    FinalExamAssignment,
    FinalExamBlock,
    FinalsSchedule,
    Section,
    SectionEnrollment,
    SectionMeeting,
    TimeSlot,
)

logger = logging.getLogger(__name__)


DAY_LABELS = {"M": "Mon", "T": "Tue", "W": "Wed", "R": "Thu", "F": "Fri"}


def _minutes_to_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _course_level(course_code: str) -> Optional[str]:
    """Return the course level indicator (leading digit of the course number).

    "BIOL101" -> "1", "CHEM3304" -> "3", "MATH1200" -> "1". Robust to 3- or
    4-digit numbering (uses the first digit either way). Returns None if the
    code carries no digits.
    """
    for ch in course_code:
        if ch.isdigit():
            return ch
    return None


@dataclass
class FinalsConfig:
    """Tunable parameters for finals scheduling."""

    # Block grid
    start_time: str = "08:30"
    block_minutes: int = 110
    gap_minutes: int = 10
    blocks_per_day: int = 4
    exam_days: list[str] = field(default_factory=lambda: ["T", "W", "R", "F"])

    # Days that are valid but discouraged. The institution's published grid uses
    # Friday only when the Tue/Wed/Thu blocks fill up.
    overflow_days: list[str] = field(default_factory=lambda: ["F"])

    # Sections starting STRICTLY AFTER this time take their final at the
    # regular meeting time (per the institution's policy for evening classes).
    evening_in_place_threshold: str = "18:00"

    # Soft objective weights (units are arbitrary but consistent across terms)
    weight_time_of_day_per_minute: int = 1
    weight_day_mismatch: int = 60
    weight_overflow_day: int = 90
    weight_two_exams_per_day: int = 200
    weight_three_exams_per_day: int = 1000
    # A student with two exams in the SAME block is a hard clash in real life,
    # resolved on the reserved conflict day. We model it as a heavily-weighted
    # soft penalty (rather than a hard constraint) so the solver stays feasible
    # and minimizes the number of such conflicts. Weighted far above the comfort
    # penalties so conflicts are eliminated first.
    weight_student_block_conflict: int = 100000

    # Fallback prior when a schedule has NO enrollment data (e.g. finals planned
    # before registration, or over projected students). With no rosters the real
    # conflict term above has nothing to act on, so we estimate likely overlap
    # from course level: students of the same level (all "1000-level", etc.) are
    # a cohort likely to share courses, while a 1000-level and a 4000-level exam
    # are unlikely to (a senior and a freshman rarely share a course). We softly
    # penalize co-scheduling same-level exams in the same block, which frees the
    # solver to stack far-apart levels. Ignored entirely when real enrollments
    # exist — actual rosters always win.
    weight_level_prior: int = 1000

    # Solver
    max_time_seconds: int = 60
    num_workers: int = 0  # 0 = auto
    log_search_progress: bool = False


@dataclass
class _SectionInfo:
    section_id: int
    primary_meeting: SectionMeeting
    primary_slot: TimeSlot
    primary_room_id: str
    class_start_min: int
    class_end_min: int
    class_days: set[str]
    is_in_place: bool


@dataclass
class _BlockInfo:
    day: str
    ordinal: int
    start_min: int
    end_min: int
    is_overflow: bool


@dataclass
class FinalsResult:
    status: str = "unknown"
    finals_schedule: Optional[FinalsSchedule] = None
    blocks: list[FinalExamBlock] = field(default_factory=list)
    assignments: list[FinalExamAssignment] = field(default_factory=list)
    objective_value: float = 0.0
    solve_time_seconds: float = 0.0
    stats: dict = field(default_factory=dict)
    # Students whose finals collide in a block (resolve on the conflict day).
    num_students_with_conflict: int = 0
    num_block_conflicts: int = 0  # total "extra" exams that must be moved
    conflict_students: list = field(default_factory=list)


class FinalsEngine:
    """CP-SAT finals exam scheduler for an existing schedule version."""

    def __init__(
        self,
        session: Session,
        schedule_id: int,
        config: FinalsConfig | None = None,
    ):
        self.session = session
        self.schedule_id = schedule_id
        self.config = config or FinalsConfig()
        self._load_data()
        self._classify_sections()
        self._generate_blocks()

    def _load_data(self):
        self.sections = list(
            self.session.exec(
                select(Section).where(Section.schedule_id == self.schedule_id)
            ).all()
        )
        section_ids = [s.id for s in self.sections]

        if section_ids:
            self.meetings = list(
                self.session.exec(
                    select(SectionMeeting).where(
                        SectionMeeting.section_id.in_(section_ids)
                    )
                ).all()
            )
            self.enrollments = list(
                self.session.exec(
                    select(SectionEnrollment).where(
                        SectionEnrollment.section_id.in_(section_ids)
                    )
                ).all()
            )
        else:
            self.meetings = []
            self.enrollments = []

        self.time_slots = list(self.session.exec(select(TimeSlot)).all())
        self.slot_by_id = {s.id: s for s in self.time_slots}

        self.meetings_by_section: dict[int, list[SectionMeeting]] = defaultdict(list)
        for m in self.meetings:
            self.meetings_by_section[m.section_id].append(m)
        for sid in self.meetings_by_section:
            self.meetings_by_section[sid].sort(key=lambda m: m.meeting_sequence)

        self.enrollments_by_section: dict[int, list[str]] = defaultdict(list)
        self.sections_by_student: dict[str, list[int]] = defaultdict(list)
        for e in self.enrollments:
            self.enrollments_by_section[e.section_id].append(e.student_id)
            self.sections_by_student[e.student_id].append(e.section_id)

        logger.info(
            "Finals engine loaded: %d sections, %d meetings, %d enrollments",
            len(self.sections), len(self.meetings), len(self.enrollments),
        )

    def _pick_in_place_day(self, class_days: set[str]) -> Optional[str]:
        """Choose a single day for an in-place final.

        Prefer a day that's also in our exam_days; otherwise fall back to
        any class day. Returns None if class_days is empty.
        """
        for d in self.config.exam_days:
            if d in class_days:
                return d
        for d in class_days:
            return d
        return None

    def _classify_sections(self):
        """Split sections into solver-scheduled, in-place, and skipped groups."""
        threshold = time_to_minutes(self.config.evening_in_place_threshold)
        self.solver_sections: list[_SectionInfo] = []
        self.in_place_sections: list[_SectionInfo] = []
        self.in_place_day_by_section: dict[int, str] = {}
        self.skipped_section_ids: set[int] = set()

        for s in self.sections:
            if s.needs_final is False:
                self.skipped_section_ids.add(s.id)
                continue

            meetings = self.meetings_by_section.get(s.id, [])
            if not meetings:
                self.skipped_section_ids.add(s.id)
                continue

            primary = meetings[0]
            slot = self.slot_by_id.get(primary.time_slot_id)
            if slot is None:
                self.skipped_section_ids.add(s.id)
                continue

            class_start = time_to_minutes(slot.start_time)
            class_end = time_to_minutes(slot.end_time)
            info = _SectionInfo(
                section_id=s.id,
                primary_meeting=primary,
                primary_slot=slot,
                primary_room_id=primary.room_id,
                class_start_min=class_start,
                class_end_min=class_end,
                class_days=set(slot.days),
                is_in_place=class_start > threshold,
            )
            if info.is_in_place:
                self.in_place_sections.append(info)
                day = self._pick_in_place_day(info.class_days)
                if day is not None:
                    self.in_place_day_by_section[s.id] = day
            else:
                self.solver_sections.append(info)

        logger.info(
            "Sections: %d solver, %d in-place, %d skipped",
            len(self.solver_sections),
            len(self.in_place_sections),
            len(self.skipped_section_ids),
        )

    def _generate_blocks(self):
        cfg = self.config
        first_start = time_to_minutes(cfg.start_time)
        step = cfg.block_minutes + cfg.gap_minutes
        overflow = set(cfg.overflow_days)

        self.blocks: list[_BlockInfo] = []
        for day in cfg.exam_days:
            for o in range(cfg.blocks_per_day):
                start = first_start + o * step
                end = start + cfg.block_minutes
                self.blocks.append(_BlockInfo(
                    day=day,
                    ordinal=o,
                    start_min=start,
                    end_min=end,
                    is_overflow=(day in overflow),
                ))

        logger.info("Generated %d candidate finals blocks", len(self.blocks))

    def _block_cost(self, sec: _SectionInfo, blk: _BlockInfo) -> int:
        """Soft cost of assigning section to block."""
        cfg = self.config
        cost = cfg.weight_time_of_day_per_minute * abs(
            sec.class_start_min - blk.start_min
        )
        if blk.day not in sec.class_days:
            cost += cfg.weight_day_mismatch
        if blk.is_overflow:
            cost += cfg.weight_overflow_day
        return cost

    def solve(self, name: str = "Finals") -> FinalsResult:
        result = FinalsResult()
        t0 = datetime.now()

        if not self.solver_sections and not self.in_place_sections:
            logger.warning("No sections to schedule finals for")
            result.status = "infeasible"
            return result

        model = cp_model.CpModel()
        n_blk = len(self.blocks)

        x: dict[tuple[int, int], cp_model.IntVar] = {}
        for si, sec in enumerate(self.solver_sections):
            for bi in range(n_blk):
                x[si, bi] = model.NewBoolVar(f"x_s{si}_b{bi}")

        for si in range(len(self.solver_sections)):
            model.AddExactlyOne([x[si, bi] for bi in range(n_blk)])

        # Hard: room sharing
        sections_by_room: dict[str, list[int]] = defaultdict(list)
        for si, sec in enumerate(self.solver_sections):
            sections_by_room[sec.primary_room_id].append(si)
        for sids in sections_by_room.values():
            if len(sids) < 2:
                continue
            for bi in range(n_blk):
                model.Add(sum(x[si, bi] for si in sids) <= 1)

        # SOFT: student same-block conflicts. A student with two exams in one
        # block is a real clash (resolved on the reserved conflict day), but
        # forbidding all of them is over-constrained and goes infeasible on
        # dense enrollments. So we count, per student per block, how many of
        # their exams land there; anything beyond the first is a conflict we
        # minimize. (An in-place evening final counts toward every block its
        # meeting time overlaps.)
        sid_to_idx = {s.section_id: i for i, s in enumerate(self.solver_sections)}
        in_place_info_by_id = {s.section_id: s for s in self.in_place_sections}

        # in-place finals occupy their regular meeting time; map student -> the
        # block indices their in-place exams overlap.
        in_place_block_base: dict[tuple[str, int], int] = defaultdict(int)
        for sec in self.in_place_sections:
            overlapping = [
                bi
                for bi, blk in enumerate(self.blocks)
                if blk.day in sec.class_days
                and blk.start_min < sec.class_end_min
                and sec.class_start_min < blk.end_min
            ]
            for student_id in self.enrollments_by_section.get(sec.section_id, []):
                for bi in overlapping:
                    in_place_block_base[(student_id, bi)] += 1

        conflict_terms: list[cp_model.IntVar] = []
        for student_id, sids in self.sections_by_student.items():
            s_idxs = [sid_to_idx[sid] for sid in sids if sid in sid_to_idx]
            if not s_idxs:
                continue
            for bi in range(n_blk):
                base = in_place_block_base.get((student_id, bi), 0)
                # Conflict only possible if total exams in this block can reach 2.
                if len(s_idxs) + base < 2:
                    continue
                ub = len(s_idxs) + base
                conf = model.NewIntVar(0, ub, f"cf_{student_id}_{bi}")
                # conf >= (exams in this block) - 1, and >= 0  => max(0, count-1)
                model.Add(conf >= sum(x[si, bi] for si in s_idxs) + base - 1)
                conflict_terms.append(conf)

        # Soft: per-student per-day exam count penalties (extra2, extra3 slacks).
        # In-place sections contribute a constant `base` to their day's count.
        in_place_day_count: dict[tuple[str, str], int] = defaultdict(int)
        for sec in self.in_place_sections:
            day = self.in_place_day_by_section.get(sec.section_id)
            if day is None:
                continue
            for student_id in self.enrollments_by_section.get(sec.section_id, []):
                in_place_day_count[(student_id, day)] += 1

        blocks_by_day: dict[str, list[int]] = defaultdict(list)
        for bi, blk in enumerate(self.blocks):
            blocks_by_day[blk.day].append(bi)

        extra2_terms: list[cp_model.IntVar] = []
        extra3_terms: list[cp_model.IntVar] = []
        for student_id, sids in self.sections_by_student.items():
            student_solver_idxs = [sid_to_idx[sid] for sid in sids if sid in sid_to_idx]
            if not student_solver_idxs and not any(
                sid in in_place_info_by_id for sid in sids
            ):
                continue
            for day, bis in blocks_by_day.items():
                base = in_place_day_count.get((student_id, day), 0)
                count_terms = [x[si, bi] for si in student_solver_idxs for bi in bis]
                ub = len(student_solver_idxs) + base
                # extra2 = max(0, count + base - 1)
                e2 = model.NewIntVar(0, max(ub, 0), f"e2_{student_id}_{day}")
                if count_terms:
                    model.Add(e2 >= sum(count_terms) + base - 1)
                else:
                    model.Add(e2 >= base - 1)
                extra2_terms.append(e2)
                # extra3 = max(0, count + base - 2)
                e3 = model.NewIntVar(0, max(ub, 0), f"e3_{student_id}_{day}")
                if count_terms:
                    model.Add(e3 >= sum(count_terms) + base - 2)
                else:
                    model.Add(e3 >= base - 2)
                extra3_terms.append(e3)

        # FALLBACK: course-level prior, used ONLY when there is no enrollment
        # data to drive the real conflict term above. Same-level exams are a
        # cohort likely to share students, so penalize 2+ of them landing in
        # the same block (mirrors the real conflict term, keyed on level cohorts
        # instead of individual students). Leaves far-apart levels free to stack.
        level_prior_terms: list[cp_model.IntVar] = []
        self._used_level_prior = False
        if not self.enrollments:
            course_code_by_section = {s.id: s.course_code for s in self.sections}
            level_to_solver_idxs: dict[str, list[int]] = defaultdict(list)
            for si, sec in enumerate(self.solver_sections):
                lvl = _course_level(course_code_by_section.get(sec.section_id, ""))
                if lvl is not None:
                    level_to_solver_idxs[lvl].append(si)
            for lvl, sids in level_to_solver_idxs.items():
                if len(sids) < 2:
                    continue  # a lone exam at this level can't collide with itself
                for bi in range(n_blk):
                    lp = model.NewIntVar(0, len(sids), f"lvl_{lvl}_{bi}")
                    # lp >= (same-level exams in this block) - 1  => max(0, count-1)
                    model.Add(lp >= sum(x[si, bi] for si in sids) - 1)
                    level_prior_terms.append(lp)
            self._used_level_prior = bool(level_prior_terms)

        # Objective
        cfg = self.config
        per_block_cost_terms = []
        for si, sec in enumerate(self.solver_sections):
            for bi, blk in enumerate(self.blocks):
                c = self._block_cost(sec, blk)
                if c > 0:
                    per_block_cost_terms.append(c * x[si, bi])

        obj_terms = list(per_block_cost_terms)
        if conflict_terms:
            obj_terms.append(cfg.weight_student_block_conflict * sum(conflict_terms))
        if level_prior_terms:
            obj_terms.append(cfg.weight_level_prior * sum(level_prior_terms))
        if extra2_terms:
            obj_terms.append(cfg.weight_two_exams_per_day * sum(extra2_terms))
        if extra3_terms:
            obj_terms.append(cfg.weight_three_exams_per_day * sum(extra3_terms))
        if obj_terms:
            model.Minimize(sum(obj_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = cfg.max_time_seconds
        # Cap auto (0) to cores-4 so the web server stays responsive mid-solve.
        solver.parameters.num_search_workers = resolve_worker_count(cfg.num_workers)
        solver.parameters.log_search_progress = cfg.log_search_progress
        status = solver.Solve(model)

        result.solve_time_seconds = (datetime.now() - t0).total_seconds()
        # Lowercase to match the semester engine ("optimal"/"feasible")
        result.status = solver.StatusName(status).lower()
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            result.objective_value = solver.ObjectiveValue()
        else:
            logger.error("Finals solver returned %s", result.status)
            return result

        self._extract_solution(result, x, solver, name)
        return result

    def _extract_solution(
        self,
        result: FinalsResult,
        x: dict,
        solver: cp_model.CpSolver,
        name: str,
    ):
        n_blk = len(self.blocks)

        # Solver section -> assigned block index.
        sec_block: dict[int, int] = {}
        for si, sec in enumerate(self.solver_sections):
            for bi in range(n_blk):
                if solver.Value(x[si, bi]) == 1:
                    sec_block[sec.section_id] = bi
                    break

        # In-place finals occupy every block their meeting time overlaps.
        ip_blocks: dict[int, set[int]] = {}
        for sec in self.in_place_sections:
            ip_blocks[sec.section_id] = {
                bi
                for bi, blk in enumerate(self.blocks)
                if blk.day in sec.class_days
                and blk.start_min < sec.class_end_min
                and sec.class_start_min < blk.end_min
            }

        label = {s.id: f"{s.course_code} {s.section_number}" for s in self.sections}

        def block_label(bi: int) -> str:
            blk = self.blocks[bi]
            return (
                f"{DAY_LABELS.get(blk.day, blk.day)} "
                f"{_minutes_to_time(blk.start_min)}-{_minutes_to_time(blk.end_min)}"
            )

        # Find students whose exams collide in a block.
        conflict_students: list[tuple[str, list[tuple[int, list[int]]]]] = []
        total_conflicts = 0
        for student_id, sids in self.sections_by_student.items():
            block_to_secs: dict[int, list[int]] = defaultdict(list)
            for sid in sids:
                if sid in sec_block:
                    block_to_secs[sec_block[sid]].append(sid)
                elif sid in ip_blocks:
                    for bi in ip_blocks[sid]:
                        block_to_secs[bi].append(sid)
            clashes = [(bi, secs) for bi, secs in block_to_secs.items() if len(secs) >= 2]
            if clashes:
                total_conflicts += sum(len(secs) - 1 for _, secs in clashes)
                conflict_students.append((student_id, clashes))

        result.num_students_with_conflict = len(conflict_students)
        result.num_block_conflicts = total_conflicts

        CAP = 500
        conflict_list = [
            {
                "student_id": student_id,
                "clashes": [
                    {"block": block_label(bi), "sections": [label.get(s, str(s)) for s in secs]}
                    for bi, secs in clashes
                ],
            }
            for student_id, clashes in conflict_students[:CAP]
        ]
        result.conflict_students = conflict_list
        logger.info(
            "Finals: %d students with a block conflict (%d total clashes)",
            len(conflict_students), total_conflicts,
        )

        cfg_dict = {
            k: v for k, v in self.config.__dict__.items() if not k.startswith("_")
        }
        fs = FinalsSchedule(
            schedule_id=self.schedule_id,
            name=name,
            created_at=datetime.now(),
            status="draft",
            config=cfg_dict,
            solver_stats={
                "status": result.status,
                "objective": result.objective_value,
                "solve_time_seconds": result.solve_time_seconds,
                "num_solver_sections": len(self.solver_sections),
                "num_in_place_sections": len(self.in_place_sections),
                "num_skipped_sections": len(self.skipped_section_ids),
                "num_blocks": len(self.blocks),
                "num_students_with_conflict": len(conflict_students),
                "num_block_conflicts": total_conflicts,
                "conflict_students": conflict_list,
                "conflict_students_truncated": len(conflict_students) > CAP,
                "level_prior_fallback_used": getattr(self, "_used_level_prior", False),
            },
        )
        self.session.add(fs)
        self.session.flush()
        result.finals_schedule = fs

        block_rows: list[FinalExamBlock] = []
        for blk in self.blocks:
            row = FinalExamBlock(
                finals_schedule_id=fs.id,
                day=blk.day,
                ordinal=blk.ordinal,
                start_time=_minutes_to_time(blk.start_min),
                end_time=_minutes_to_time(blk.end_min),
                label=(
                    f"{DAY_LABELS.get(blk.day, blk.day)} "
                    f"{_minutes_to_time(blk.start_min)}-{_minutes_to_time(blk.end_min)}"
                ),
            )
            self.session.add(row)
            block_rows.append(row)
        self.session.flush()
        result.blocks = block_rows

        assignments: list[FinalExamAssignment] = []
        for sec in self.solver_sections:
            bi = sec_block.get(sec.section_id)
            if bi is None:
                continue
            a = FinalExamAssignment(
                finals_schedule_id=fs.id,
                section_id=sec.section_id,
                block_id=block_rows[bi].id,
                room_id=sec.primary_room_id,
                is_in_place=False,
            )
            self.session.add(a)
            assignments.append(a)

        for sec in self.in_place_sections:
            a = FinalExamAssignment(
                finals_schedule_id=fs.id,
                section_id=sec.section_id,
                block_id=None,
                room_id=sec.primary_room_id,
                is_in_place=True,
                in_place_time_slot_id=sec.primary_slot.id,
            )
            self.session.add(a)
            assignments.append(a)

        self.session.commit()
        result.assignments = assignments
        logger.info("Persisted %d final exam assignments", len(assignments))


class FinalsExporter:
    """Exports finals schedules as CSV or Excel."""

    def __init__(self, session: Session):
        self.session = session

    def _load(self, finals_schedule_id: int):
        from src.database.models import (
            Course,
            Faculty,
            Room,
            Section,
        )
        fs = self.session.get(FinalsSchedule, finals_schedule_id)
        if not fs:
            raise ValueError(f"FinalsSchedule {finals_schedule_id} not found")

        blocks = list(self.session.exec(
            select(FinalExamBlock).where(
                FinalExamBlock.finals_schedule_id == finals_schedule_id
            )
        ).all())
        block_by_id = {b.id: b for b in blocks}

        assignments = list(self.session.exec(
            select(FinalExamAssignment).where(
                FinalExamAssignment.finals_schedule_id == finals_schedule_id
            )
        ).all())

        section_ids = [a.section_id for a in assignments]
        sections = list(self.session.exec(
            select(Section).where(Section.id.in_(section_ids))
        ).all()) if section_ids else []
        section_by_id = {s.id: s for s in sections}

        courses = {c.code: c for c in self.session.exec(select(Course)).all()}
        rooms = {r.id: r for r in self.session.exec(select(Room)).all()}
        faculty = {f.id: f for f in self.session.exec(select(Faculty)).all()}
        time_slots = {t.id: t for t in self.session.exec(select(TimeSlot)).all()}
        meetings = list(self.session.exec(
            select(SectionMeeting).where(SectionMeeting.section_id.in_(section_ids))
        ).all()) if section_ids else []
        primary_meeting_by_section: dict[int, SectionMeeting] = {}
        for m in meetings:
            cur = primary_meeting_by_section.get(m.section_id)
            if cur is None or m.meeting_sequence < cur.meeting_sequence:
                primary_meeting_by_section[m.section_id] = m

        return {
            "finals_schedule": fs,
            "blocks": blocks,
            "block_by_id": block_by_id,
            "assignments": assignments,
            "section_by_id": section_by_id,
            "courses": courses,
            "rooms": rooms,
            "faculty": faculty,
            "time_slots": time_slots,
            "primary_meeting_by_section": primary_meeting_by_section,
        }

    def export_csv(
        self,
        finals_schedule_id: int,
        output_path: "Path | str | None" = None,
    ) -> str:
        """Export finals schedule to CSV. Returns CSV string content."""
        import csv
        from io import StringIO
        from pathlib import Path

        data = self._load(finals_schedule_id)
        out = StringIO()
        w = csv.writer(out)
        w.writerow([
            "CRN", "COURSE", "SECTION", "TITLE", "INSTRUCTOR",
            "EXAM_DAY", "EXAM_START", "EXAM_END", "ROOM", "IS_IN_PLACE",
        ])

        # Chronological day order for sorting — labels sort Fri < Mon < …
        # alphabetically. Handles both block labels ("Tue") and in-place
        # multi-day strings ("M,W,F" — sorted by their first day).
        week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        code_order = {"M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6}

        def day_sort(day_str: str) -> int:
            first = day_str.split(",")[0].strip()
            if first in week:
                return week.index(first)
            return code_order.get(first, 99)

        rows = []
        for a in data["assignments"]:
            sec = data["section_by_id"].get(a.section_id)
            if not sec:
                continue
            course = data["courses"].get(sec.course_code)
            fac = data["faculty"].get(sec.faculty_id) if sec.faculty_id else None
            room = data["rooms"].get(a.room_id)
            room_label = f"{room.building} {room.number}" if room else a.room_id
            if a.is_in_place and a.in_place_time_slot_id is not None:
                ts = data["time_slots"].get(a.in_place_time_slot_id)
                day = ",".join(ts.days) if ts else ""
                start, end = (ts.start_time, ts.end_time) if ts else ("", "")
            else:
                blk = data["block_by_id"].get(a.block_id)
                day = DAY_LABELS.get(blk.day, blk.day) if blk else ""
                start = blk.start_time if blk else ""
                end = blk.end_time if blk else ""
            rows.append([
                sec.crn, sec.course_code, sec.section_number,
                course.title if course else "",
                fac.name if fac else "",
                day, start, end, room_label,
                "Y" if a.is_in_place else "N",
            ])

        rows.sort(key=lambda r: (day_sort(r[5]), r[6], r[1], r[2]))
        for r in rows:
            w.writerow(r)

        content = out.getvalue()
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content)
            logger.info("Wrote finals CSV to %s", output_path)
        return content

    def export_excel(
        self,
        finals_schedule_id: int,
        output_path: "Path | str",
    ) -> "Path":
        """Export finals schedule to a single-sheet Excel workbook."""
        from openpyxl import Workbook

        from src.core.xlsx_util import write_csv_sheet

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        csv_content = self.export_csv(finals_schedule_id)

        wb = Workbook()
        ws = wb.active
        ws.title = "Finals Schedule"
        write_csv_sheet(ws, csv_content)

        wb.save(output_path)
        logger.info("Wrote finals Excel to %s", output_path)
        return output_path
