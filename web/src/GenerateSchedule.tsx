import { useState } from "react";
import { api } from "./api";
import { useSolverStream } from "./useSolverStream";
import { SolverLog } from "./SolverLog";
import { SolverViz } from "./SolverViz";
import { useOptimizationStatus } from "./optimizationStatus";

export function GenerateSchedule({ onCreated }: { onCreated?: (scheduleId: number) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("Generated Schedule");
  const [semester, setSemester] = useState("2025SP");
  const [timeLimit, setTimeLimit] = useState(120);
  const [includeProjections, setIncludeProjections] = useState(false);
  // Class-size knobs; blank/"" = use the server's config defaults.
  const [classSizeCap, setClassSizeCap] = useState("");
  const [capacityMode, setCapacityMode] = useState<"" | "fixed" | "room">("");
  const [conflictThreshold, setConflictThreshold] = useState("");

  const stream = useSolverStream();
  const optStatus = useOptimizationStatus();
  // Busy because of someone else's run (not this component's own stream).
  const busyElsewhere = optStatus.running && !stream.running;

  function start() {
    stream.start(
      api.generateStreamUrl({
        semester,
        name,
        time_limit: timeLimit,
        include_projections: includeProjections,
        class_size_cap: classSizeCap ? Number(classSizeCap) : undefined,
        capacity_mode: capacityMode || undefined,
        conflict_threshold: conflictThreshold !== "" ? Number(conflictThreshold) : undefined,
      }),
      (done) => {
        if (done.schedule_id != null) onCreated?.(done.schedule_id);
      }
    );
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="row">
        <button onClick={() => setOpen((o) => !o)} className={open ? "ghost" : ""}>
          {open ? "Hide" : "Generate Schedule"}
        </button>
        {!open && <span className="subtle">Run the solver to build a new schedule.</span>}
      </div>

      {open && (
        <div style={{ marginTop: 12 }}>
          <div className="row" style={{ marginBottom: 10 }}>
            <div>
              <label htmlFor="gen-name">Name</label>
              <input
                id="gen-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={stream.running}
                style={{ width: 200 }}
              />
            </div>
            <div>
              <label htmlFor="gen-sem">Semester</label>
              <input
                id="gen-sem"
                value={semester}
                onChange={(e) => setSemester(e.target.value)}
                disabled={stream.running}
                style={{ width: 100 }}
              />
            </div>
            <div>
              <label htmlFor="gen-time">Time limit (s)</label>
              <input
                id="gen-time"
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
                Start
              </button>
            )}
          </div>

          <div className="row" style={{ marginBottom: 10 }}>
            <div title="Cap every class at this many seats for this run (blank = config default; a course's own max_class_size from the catalog still applies).">
              <label htmlFor="gen-cap">Class size cap</label>
              <input
                id="gen-cap"
                type="number"
                min={1}
                max={500}
                placeholder="default"
                value={classSizeCap}
                onChange={(e) => setClassSizeCap(e.target.value)}
                disabled={stream.running}
                style={{ width: 90 }}
              />
            </div>
            <div title="Fixed: every section seats the cap. Room: class sizes float — a section may fill up to its assigned room's capacity (within caps), and the saved capacity reflects the room chosen.">
              <label htmlFor="gen-mode">Capacity mode</label>
              <select
                id="gen-mode"
                value={capacityMode}
                onChange={(e) => setCapacityMode(e.target.value as "" | "fixed" | "room")}
                disabled={stream.running}
              >
                <option value="">Config default</option>
                <option value="fixed">Fixed section size</option>
                <option value="room">Float to the room</option>
              </select>
            </div>
            <div title="Course pairs whose shared-student urgency sum exceeds this are penalized for overlapping in time. Lower = stricter about student conflicts; small cohorts can afford stricter values (the config default is 50). 0 = penalize every pair. Blank = config default.">
              <label htmlFor="gen-conflict">Conflict threshold</label>
              <input
                id="gen-conflict"
                type="number"
                min={0}
                placeholder="default"
                value={conflictThreshold}
                onChange={(e) => setConflictThreshold(e.target.value)}
                disabled={stream.running}
                style={{ width: 90 }}
              />
            </div>
          </div>

          {busyElsewhere && (
            <div className="subtle" style={{ marginBottom: 10 }}>
              Another optimization is running — please wait for it to finish.
            </div>
          )}

          <label
            style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}
            title="Adds synthetic demand for the projected incoming freshman class (set on the Projections screen) so intro courses are sized for students not yet in Banner."
          >
            <input
              type="checkbox"
              checked={includeProjections}
              onChange={(e) => setIncludeProjections(e.target.checked)}
              disabled={stream.running}
              style={{ width: "auto" }}
            />
            <span>Include projected freshman class</span>
          </label>

          <SolverViz stream={stream} />
          <SolverLog stream={stream} />
        </div>
      )}
    </div>
  );
}
