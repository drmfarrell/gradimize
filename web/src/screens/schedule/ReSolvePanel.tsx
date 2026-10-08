import { useState } from "react";
import { api } from "../../api";
import { useOptimizationStatus } from "../../optimizationStatus";
import { SolverLog } from "../../SolverLog";
import { SolverViz } from "../../SolverViz";
import { useSolverStream } from "../../useSolverStream";

// Collapsible re-solve / repair control: streams live solver progress and,
// for repair, shows what moved vs the base schedule. Owns its own stream
// state; tells the parent when a new schedule is created.
export function ReSolvePanel({
  scheduleId,
  pinnedCount,
  attention,
  onCreated,
}: {
  scheduleId: number;
  pinnedCount: number;
  attention?: boolean;
  onCreated: (scheduleId: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [timeLimit, setTimeLimit] = useState(120);
  const [mode, setMode] = useState<"re-solve" | "repair">("re-solve");
  const stream = useSolverStream();
  const optStatus = useOptimizationStatus();
  const busyElsewhere = optStatus.running && !stream.running;

  function start() {
    const url =
      mode === "repair"
        ? api.repairStreamUrl(scheduleId, { time_limit: timeLimit })
        : api.reSolveStreamUrl(scheduleId, { time_limit: timeLimit });
    stream.start(url, (done) => {
      if (done.schedule_id != null) onCreated(done.schedule_id);
    });
  }

  const diff = stream.result?.repair_diff;

  return (
    <div
      className="card"
      style={{
        marginBottom: 16,
        border: attention && !open ? "1px solid var(--accent)" : undefined,
      }}
    >
      <div className="row">
        <button onClick={() => setOpen((o) => !o)} className={open || !attention ? "ghost" : ""}>
          {open ? "Hide" : "Re-solve / Repair"}
        </button>
        <span className="subtle">
          {mode === "repair"
            ? "Something broke after publishing? Repair keeps every section where it is and moves only what current data forces."
            : `Keeps ${pinnedCount} pinned section${pinnedCount === 1 ? "" : "s"} fixed and re-optimizes the rest into a new schedule.`}
        </span>
      </div>

      {open && (
        <div style={{ marginTop: 12 }}>
          <div className="row" style={{ marginBottom: 10 }}>
            <div>
              <label htmlFor="rs-mode">Mode</label>
              <select
                id="rs-mode"
                value={mode}
                onChange={(e) => setMode(e.target.value as "re-solve" | "repair")}
                disabled={stream.running}
              >
                <option value="re-solve">Re-solve (re-optimize unpinned)</option>
                <option value="repair">Repair (minimal changes only)</option>
              </select>
            </div>
            <div>
              <label htmlFor="rs-time">Time limit (s)</label>
              <input
                id="rs-time"
                type="number"
                min={5}
                max={1800}
                value={timeLimit}
                onChange={(e) => setTimeLimit(Number(e.target.value))}
                disabled={stream.running}
                style={{ width: 90 }}
              />
            </div>
            <div className="spacer" />
            {stream.running ? (
              <button onClick={stream.stop} className="ghost">
                Stop watching
              </button>
            ) : (
              <button onClick={start} disabled={busyElsewhere}>
                {mode === "repair" ? "Start repair" : "Start re-solve"}
              </button>
            )}
          </div>
          {busyElsewhere && (
            <div className="subtle" style={{ marginBottom: 10 }}>
              Another optimization is running — please wait for it to finish.
            </div>
          )}
          <SolverViz stream={stream} />
          <SolverLog
            stream={stream}
            doneNote={
              mode === "repair"
                ? "Repaired into a new schedule — the original is untouched."
                : "Re-optimized into a new schedule."
            }
          />
          {diff && (
            <div style={{ marginTop: 10 }}>
              <strong>
                Repair result: {diff.unchanged_count} section
                {diff.unchanged_count === 1 ? "" : "s"} untouched,{" "}
                {diff.changed.length} changed
                {diff.removed.length > 0 ? `, ${diff.removed.length} dropped` : ""}.
              </strong>
              {diff.changed.length > 0 && (
                <ul style={{ margin: "6px 0 0 18px" }}>
                  {diff.changed.map((c) => (
                    <li key={`${c.course_code}-${c.section_number}`}>
                      {c.course_code}-{c.section_number}:{" "}
                      {Object.entries(c.changes)
                        .map(
                          ([field, d]) => `${field} ${d.from ?? "—"} → ${d.to ?? "—"}`
                        )
                        .join("; ")}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
