import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { sortNewestFirst, fmtTimestamp } from "../format";
import type {
  FinalsConflictStudent,
  FinalsScheduleDetail,
  FinalsSolveResponse,
  FinalsSolverStats,
} from "../api";
import { useAsync } from "../useAsync";
import { Spinner, ErrorBox, Empty, StatusPill } from "../components";
import { useOptimizationStatus } from "../optimizationStatus";

export function FinalsView() {
  const { scheduleId } = useParams();
  const navigate = useNavigate();

  const schedules = useAsync(useCallback(() => api.listSchedules(), []), []);

  const selectedScheduleId = useMemo(() => {
    if (scheduleId) return Number(scheduleId);
    if (schedules.data && schedules.data.length > 0) {
      return sortNewestFirst(schedules.data)[0].id;
    }
    return null;
  }, [scheduleId, schedules.data]);

  const finalsList = useAsync(
    useCallback(
      () =>
        selectedScheduleId == null
          ? Promise.resolve([])
          : api.listFinalsForSchedule(selectedScheduleId),
      [selectedScheduleId]
    ),
    [selectedScheduleId]
  );

  const [selectedFinalsId, setSelectedFinalsId] = useState<number | null>(null);
  const [solving, setSolving] = useState(false);
  const [solveMsg, setSolveMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // Reset when the schedule changes via the URL too (back/forward, nav link) —
  // the component instance survives those, so onChange alone isn't enough and
  // a stale finals run from the previous schedule would stay on screen.
  // Adjusted during render rather than in an effect (React's "resetting
  // state when a prop changes" pattern).
  const [prevScheduleId, setPrevScheduleId] = useState(selectedScheduleId);
  if (selectedScheduleId !== prevScheduleId) {
    setPrevScheduleId(selectedScheduleId);
    setSelectedFinalsId(null);
    setSolveMsg(null);
  }
  const optStatus = useOptimizationStatus();
  // Busy because of another run (not this view's own in-flight solve).
  const busyElsewhere = optStatus.running && !solving;

  // The finals schedule to display: explicit selection, else newest in list.
  const activeFinalsId = useMemo(() => {
    if (selectedFinalsId != null) return selectedFinalsId;
    if (finalsList.data && finalsList.data.length > 0) {
      return sortNewestFirst(finalsList.data)[0].id;
    }
    return null;
  }, [selectedFinalsId, finalsList.data]);

  const detail = useAsync(
    useCallback(
      () => (activeFinalsId == null ? Promise.resolve(null) : api.getFinals(activeFinalsId)),
      [activeFinalsId]
    ),
    [activeFinalsId]
  );

  async function handleSolve() {
    if (selectedScheduleId == null) return;
    setSolving(true);
    setSolveMsg(null);
    try {
      const res: FinalsSolveResponse = await api.solveFinals(selectedScheduleId, {
        name: "Web Finals",
      });
      setSolveMsg({
        ok: true,
        text:
          `${res.status} — ${res.num_solver_sections} exams scheduled in ` +
          `${res.solve_time_seconds.toFixed(1)}s · ` +
          `${res.num_students_with_conflict} students with a conflict ` +
          `(${res.num_block_conflicts} clashes) to resolve on the conflict day`,
      });
      setSelectedFinalsId(res.finals_schedule_id);
      finalsList.reload();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      setSolveMsg({ ok: false, text: msg });
    } finally {
      setSolving(false);
    }
  }

  return (
    <div>
      <h1>Finals</h1>

      <div className="row" style={{ marginBottom: 16 }}>
        <div>
          <label htmlFor="fin-sched">Semester schedule</label>
          <select
            id="fin-sched"
            value={selectedScheduleId ?? ""}
            onChange={(e) => {
              setSelectedFinalsId(null);
              setSolveMsg(null);
              navigate(`/finals/${e.target.value}`);
            }}
            disabled={!schedules.data || schedules.data.length === 0}
          >
            {schedules.data &&
              sortNewestFirst(schedules.data).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.semester})
                </option>
              ))}
          </select>
        </div>

        {finalsList.data && finalsList.data.length > 0 && (
          <div>
            <label htmlFor="fin-run">Finals run</label>
            <select
              id="fin-run"
              value={activeFinalsId ?? ""}
              onChange={(e) => setSelectedFinalsId(Number(e.target.value))}
            >
              {sortNewestFirst(finalsList.data)
                .map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name} — {fmtTimestamp(f.created_at)} ({f.num_assignments})
                  </option>
                ))}
            </select>
          </div>
        )}

        <div className="spacer" />
        <button
          onClick={handleSolve}
          disabled={solving || busyElsewhere || selectedScheduleId == null}
          title={busyElsewhere ? "Another optimization is running — please wait." : undefined}
        >
          {solving ? "Solving…" : busyElsewhere ? "Optimizer busy…" : "Solve Finals"}
        </button>
        {activeFinalsId != null && (
          <a className="btn ghost" href={api.finalsCsvUrl(activeFinalsId)}>
            Export CSV
          </a>
        )}
      </div>

      {solveMsg && (
        <div className={solveMsg.ok ? "banner banner-ok" : "banner banner-err"}>{solveMsg.text}</div>
      )}

      {schedules.loading && <Spinner />}
      {schedules.error && <ErrorBox message={schedules.error} />}
      {schedules.data && schedules.data.length === 0 && (
        <Empty>No semester schedules found. Generate one first.</Empty>
      )}

      {selectedScheduleId != null && finalsList.data && finalsList.data.length === 0 && (
        <Empty>No finals scheduled yet for this semester. Press “Solve Finals”.</Empty>
      )}

      {detail.loading && <Spinner />}
      {detail.error && <ErrorBox message={detail.error} />}
      {detail.data && <FinalsDetailTable detail={detail.data} />}
    </div>
  );
}

function FinalsDetailTable({ detail }: { detail: FinalsScheduleDetail }) {
  const rows = detail.assignments
    .slice()
    .sort(
      (a, b) =>
        (a.block_label ?? "zzz").localeCompare(b.block_label ?? "zzz") ||
        a.course_code.localeCompare(b.course_code)
    );

  const stats = detail.solver_stats;

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>{detail.name}</h2>
        <StatusPill status={detail.status} />
        <span className="subtle">
          {detail.assignments.length} exams · {detail.blocks.length} blocks
        </span>
        {stats.solve_time_seconds != null && (
          <span className="subtle">{stats.solve_time_seconds.toFixed(1)}s</span>
        )}
      </div>

      <ConflictSummary stats={stats} />

      {rows.length === 0 ? (
        <Empty>No exam assignments in this finals run.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>CRN</th>
                <th>Course</th>
                <th>Sec</th>
                <th>Exam Block</th>
                <th>Room</th>
                <th>Type</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.section_id}>
                  <td className="mono">{a.crn}</td>
                  <td>{a.course_code}</td>
                  <td>{a.section_number}</td>
                  <td>
                    {a.is_in_place
                      ? `${a.in_place_time_slot_name ?? "regular meeting"} (in place)`
                      : a.block_label ?? "—"}
                  </td>
                  <td>{a.room_id}</td>
                  <td>
                    {a.is_in_place ? (
                      <span className="pill pill-archived">In-place</span>
                    ) : (
                      <span className="pill pill-approved">Block</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ConflictSummary({ stats }: { stats: FinalsSolverStats }) {
  const n = stats.num_students_with_conflict ?? 0;
  const clashes = stats.num_block_conflicts ?? 0;
  const students: FinalsConflictStudent[] = stats.conflict_students ?? [];
  const truncated = Boolean(stats.conflict_students_truncated);
  const fallbackUsed = Boolean(stats.level_prior_fallback_used);

  // Fallback mode: no enrollment data, so conflicts couldn't be checked against
  // real rosters. Exam times were spread using course level as a proxy. This
  // takes precedence over the "no conflicts" banner, which would otherwise
  // imply a verified-clean result.
  if (fallbackUsed) {
    return (
      <div
        className="banner"
        style={{
          marginBottom: 12,
          background: "rgba(88,166,255,0.1)",
          border: "1px solid rgba(88,166,255,0.35)",
        }}
      >
        <div style={{ color: "var(--blue, #58a6ff)" }}>
          ℹ No enrollment data for this schedule — student conflicts could not be
          checked against real rosters. Exam times were spread using{" "}
          <strong>course level as a fallback</strong> (same-level exams kept apart,
          since a senior and a freshman rarely share a course). Re-run once
          registrations are imported to minimize and report actual conflicts.
        </div>
      </div>
    );
  }

  if (n === 0) {
    return (
      <div className="banner banner-ok" style={{ marginBottom: 12 }}>
        ✓ No student conflicts — every student's exams are in different blocks.
      </div>
    );
  }

  return (
    <div
      className="banner"
      style={{
        marginBottom: 12,
        background: "rgba(210,153,34,0.1)",
        border: "1px solid rgba(210,153,34,0.3)",
      }}
    >
      <div style={{ color: "var(--yellow)", marginBottom: 4 }}>
        ⚠ {n} student{n === 1 ? "" : "s"} have a conflict ({clashes} clash
        {clashes === 1 ? "" : "es"}) — two exams in the same block. Resolve these on the
        reserved conflict day (Friday) or via make-up slots.
      </div>
      <details>
        <summary className="subtle" style={{ cursor: "pointer" }}>
          Show affected students{truncated ? ` (first ${students.length})` : ""}
        </summary>
        <div className="table-wrap" style={{ maxHeight: "40vh", overflow: "auto", marginTop: 6 }}>
          <table>
            <thead>
              <tr>
                <th>Student</th>
                <th>Block</th>
                <th>Colliding exams</th>
              </tr>
            </thead>
            <tbody>
              {students.flatMap((s) =>
                s.clashes.map((c, i) => (
                  <tr key={`${s.student_id}-${i}`}>
                    {i === 0 ? (
                      <td className="mono" rowSpan={s.clashes.length}>
                        {s.student_id}
                      </td>
                    ) : null}
                    <td>{c.block}</td>
                    <td>{c.sections.join("  ×  ")}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
