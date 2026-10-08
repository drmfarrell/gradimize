import { useEffect, useMemo, useRef, useState } from "react";
import type { ActivityEvent, IncumbentEvent, PhaseEvent, VizEvent, VizInitEvent, VizSlot } from "./useSolverStream";
import "./SolverViz.css";

// Braille spinner frames, one per received tick — advances only when the
// stream actually delivers a tick, never on a timer, so it can't lie about
// a dead connection.
const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const CONSOLE_LINES = 6;
const STALE_AFTER_MS = 5000;

// Colorblind-safe palette, must match the TUI exactly (same order).
const PALETTE = [
  "#E69F00",
  "#56B4E9",
  "#009E73",
  "#F0E442",
  "#0072B2",
  "#D55E00",
  "#CC79A7",
  "#999999",
  "#8DD3C7",
  "#BC80BD",
  "#B15928",
  "#7FC97F",
];

const DAYS = ["M", "T", "W", "R", "F"];
const DAY_LABEL: Record<string, string> = { M: "Mon", T: "Tue", W: "Wed", R: "Thu", F: "Fri" };

const PHASE_LABEL: Record<string, string> = {
  presolve: "Setting up",
  searching: "Searching",
  first_solution: "First schedule found",
  improving: "Improving",
  certifying: "Certifying (proving optimality)",
  done: "Done",
};

function fmtMSS(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

interface SeriesPoint {
  t: number;
  objective?: number;
  bound?: number;
}

interface ConsoleLine {
  tick: number;
  line: string;
}

interface FoldedViz {
  vizInit: VizInitEvent | null;
  phase: PhaseEvent | null;
  series: SeriesPoint[];
  placed: [number, number, string][] | null;
  solutions: number;
  moved?: number;
  conflicts?: number;
  branches?: number;
  restarts?: number;
  quietFor: number | null;
  gapPct?: number;
  t: number;
  done: boolean;
  // "Machine room" liveness: tick advances on every emitted server event
  // (proves the stream is alive); logLines counts CP-SAT's own output
  // (proves the solver is doing work — these are NOT the same fact).
  tick: number;
  logLines: number;
  consoleLines: ConsoleLine[];
  // Sticks around after the "searching" phase event passes so the label
  // can keep showing the worker count.
  workers: number | null;
}

function foldViz(events: VizEvent[]): FoldedViz {
  let vizInit: VizInitEvent | null = null;
  let phase: PhaseEvent | null = null;
  const series: SeriesPoint[] = [];
  let placed: [number, number, string][] | null = null;
  let solutions = 0;
  let moved: number | undefined;
  let conflicts: number | undefined;
  let branches: number | undefined;
  let restarts: number | undefined;
  let quietFor: number | null = null;
  let gapPct: number | undefined;
  let t = 0;
  let done = false;
  let tick = 0;
  let logLines = 0;
  const consoleLines: ConsoleLine[] = [];
  let workers: number | null = null;

  for (const e of events) {
    if (e.type === "viz_init") {
      vizInit = e;
    } else if (e.type === "phase") {
      phase = e;
      t = e.t;
      if (e.solutions != null) solutions = e.solutions;
      if (e.gap_pct != null) gapPct = e.gap_pct;
      if (e.workers != null) workers = e.workers;
      if (e.phase === "done") done = true;
    } else if (e.type === "incumbent") {
      const ie: IncumbentEvent = e;
      t = ie.t;
      solutions = ie.n;
      gapPct = ie.gap_pct;
      series.push({ t: ie.t, objective: ie.objective, bound: ie.bound });
      if (ie.conflicts != null) conflicts = ie.conflicts;
      if (ie.branches != null) branches = ie.branches;
      if (ie.moved != null) moved = ie.moved;
      if (ie.placed) placed = ie.placed;
    } else if (e.type === "bound") {
      t = e.t;
      if (e.gap_pct != null) gapPct = e.gap_pct;
      series.push({ t: e.t, bound: e.bound, objective: e.objective });
    } else if (e.type === "activity") {
      const ae: ActivityEvent = e;
      t = ae.t;
      solutions = ae.solutions;
      quietFor = ae.quiet_for;
      if (ae.conflicts != null) conflicts = ae.conflicts;
      if (ae.branches != null) branches = ae.branches;
      if (ae.restarts != null) restarts = ae.restarts;
      if (ae.gap_pct != null) gapPct = ae.gap_pct;
      if (ae.tick != null) tick = ae.tick;
      logLines = ae.log_lines;
    } else if (e.type === "solver_line") {
      t = e.t;
      tick = e.tick;
      logLines = e.log_lines;
      consoleLines.push({ tick: e.tick, line: e.line });
      if (consoleLines.length > CONSOLE_LINES) consoleLines.shift();
    }
  }

  return {
    vizInit,
    phase,
    series,
    placed,
    solutions,
    moved,
    conflicts,
    branches,
    restarts,
    quietFor,
    gapPct,
    t,
    done,
    tick,
    logLines,
    consoleLines,
    workers,
  };
}

// Rows of the week grid: one per distinct (start,end) time, each mapping a
// weekday to whichever viz_init slot actually covers it (MWF and TR share a
// row when their times match, even though they're different slot ids).
function buildRows(slots: VizSlot[]) {
  const sorted = [...slots].sort((a, b) => a.start.localeCompare(b.start) || a.pattern.localeCompare(b.pattern));
  const order: string[] = [];
  const labelByKey = new Map<string, { start: string; end: string }>();
  const slotForDay = new Map<string, Map<string, number>>();
  for (const s of sorted) {
    const key = `${s.start}-${s.end}`;
    if (!labelByKey.has(key)) {
      labelByKey.set(key, { start: s.start, end: s.end });
      order.push(key);
    }
    let dayMap = slotForDay.get(key);
    if (!dayMap) {
      dayMap = new Map();
      slotForDay.set(key, dayMap);
    }
    for (const d of s.days) {
      if (DAYS.includes(d) && !dayMap.has(d)) dayMap.set(d, s.id);
    }
  }
  return order.map((key) => ({
    key,
    ...labelByKey.get(key)!,
    slotForDay: slotForDay.get(key) ?? new Map<string, number>(),
  }));
}

function sigBySlotOf(placed: [number, number, string][]): Map<number, string> {
  const sigBySlot = new Map<number, string>();
  for (const [slotId, deptIdx, label] of placed) {
    sigBySlot.set(slotId, (sigBySlot.get(slotId) ?? "") + `|${deptIdx}:${label}`);
  }
  return sigBySlot;
}

// Slot ids whose contents differ between two `placed` snapshots.
function diffPlacedSlots(
  prevPlaced: [number, number, string][],
  placed: [number, number, string][]
): Set<number> {
  const prev = sigBySlotOf(prevPlaced);
  const cur = sigBySlotOf(placed);
  const changed = new Set<number>();
  for (const [slotId, sig] of cur) if (prev.get(slotId) !== sig) changed.add(slotId);
  for (const slotId of prev.keys()) if (!cur.has(slotId)) changed.add(slotId);
  return changed;
}

export function SolverViz({ stream }: { stream: { vizEvents: VizEvent[] } }) {
  const folded = useMemo(() => foldViz(stream.vizEvents), [stream.vizEvents]);
  const {
    vizInit,
    phase,
    series,
    placed,
    solutions,
    moved,
    conflicts,
    branches,
    restarts,
    quietFor,
    gapPct,
    t,
    done,
    tick,
    logLines,
    consoleLines,
    workers,
  } = folded;

  // Stale detection: wall-clock time since the last event actually arrived
  // (independent of solver time `t`, which is server-reported and can't
  // tell us the connection died). `staleMs` is recomputed once a second in
  // an effect so the "no signal for Ns" text keeps counting up — it never
  // drives the spinner.
  const lastEventAtRef = useRef<number | null>(null);
  const prevLenRef = useRef(0);
  const [staleMs, setStaleMs] = useState(0);
  useEffect(() => {
    if (lastEventAtRef.current == null || stream.vizEvents.length !== prevLenRef.current) {
      lastEventAtRef.current = Date.now();
      prevLenRef.current = stream.vizEvents.length;
    }
  }, [stream.vizEvents]);
  useEffect(() => {
    const id = window.setInterval(() => {
      setStaleMs(Date.now() - (lastEventAtRef.current ?? Date.now()));
    }, 1000);
    return () => window.clearInterval(id);
  }, []);
  const stale = staleMs > STALE_AFTER_MS;

  // Highlight tiles whose slot's contents changed since the last snapshot.
  // Diffed during render against the previous `placed` snapshot (React's
  // "adjust state when a prop changes" pattern), since it's a pure
  // derivation of the placed-tiles data rather than an external subscription.
  const [prevPlaced, setPrevPlaced] = useState(placed);
  const [flashSlots, setFlashSlots] = useState<Set<number>>(new Set());
  if (placed !== prevPlaced) {
    if (placed && prevPlaced) {
      const changed = diffPlacedSlots(prevPlaced, placed);
      if (changed.size) setFlashSlots(changed);
    }
    setPrevPlaced(placed);
  }
  useEffect(() => {
    if (flashSlots.size === 0) return;
    const id = window.setTimeout(() => setFlashSlots(new Set()), 650);
    return () => window.clearTimeout(id);
  }, [flashSlots]);

  const rows = useMemo(() => (vizInit ? buildRows(vizInit.slots) : []), [vizInit]);

  const placementsBySlot = useMemo(() => {
    const m = new Map<number, { deptIdx: number; label: string }[]>();
    if (placed) {
      for (const [slotId, deptIdx, label] of placed) {
        const arr = m.get(slotId) ?? [];
        arr.push({ deptIdx, label });
        m.set(slotId, arr);
      }
    }
    return m;
  }, [placed]);

  // Hard requirement: no viz_init ever arrived -> render nothing.
  if (!vizInit) return null;

  const timeLimit = Math.max(1, vizInit.time_limit);
  const pct = done ? 1 : Math.min(1, Math.max(0, t / timeLimit));
  const phaseKey = phase?.phase ?? (done ? "done" : "presolve");
  const phaseLabel =
    phaseKey === "searching" && workers != null
      ? `${PHASE_LABEL.searching} · ${workers} workers`
      : PHASE_LABEL[phaseKey] ?? phaseKey;

  // Convergence strip geometry.
  const W = 640;
  const H = 120;
  const maxT = Math.max(1, ...series.map((p) => p.t), t);
  const values = series.flatMap((p) => [p.objective, p.bound]).filter((v): v is number => v != null);
  const minV = values.length ? Math.min(...values) : 0;
  const maxV = values.length ? Math.max(...values) : 1;
  const span = maxV - minV || 1;
  const xOf = (pt: number) => (pt / maxT) * W;
  const yOf = (v: number) => H - ((v - minV) / span) * H;
  const objPath = series
    .filter((p) => p.objective != null)
    .map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.t).toFixed(1)},${yOf(p.objective as number).toFixed(1)}`)
    .join(" ");
  const boundPath = series
    .filter((p) => p.bound != null)
    .map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.t).toFixed(1)},${yOf(p.bound as number).toFixed(1)}`)
    .join(" ");

  const lastImproved = quietFor != null ? `last improved ${fmtMSS(quietFor)} ago` : null;

  // No incumbent with a placement has landed yet — the grid would otherwise
  // render as an empty table and read as stalled. Word the message from the
  // actual phase rather than a generic spinner-less "loading" placeholder.
  let gridPlaceholder: string | null = null;
  if (!placed) {
    if (phaseKey === "presolve") {
      gridPlaceholder = "Simplifying the problem — no schedule to show yet";
    } else if (phaseKey === "searching") {
      gridPlaceholder =
        workers != null
          ? `Searching for a first workable schedule · ${workers} workers`
          : "Searching for a first workable schedule";
    } else {
      gridPlaceholder = "Waiting for the first schedule";
    }
  }

  return (
    <div className="solver-viz">
      {/* 1. Top progress bar */}
      <div className="viz-progress">
        <div className="viz-progress-track">
          <div className="viz-progress-fill" style={{ width: `${pct * 100}%` }} />
        </div>
        <div className="viz-progress-meta">
          <span className="mono">
            {fmtMSS(t)} / {fmtMSS(timeLimit)}
          </span>
          <span className="viz-phase">{phaseLabel}</span>
          {gapPct != null && <span className="pill viz-gap-badge">gap {gapPct.toFixed(1)}%</span>}
        </div>
      </div>

      {/* 2. Convergence strip */}
      <div className="viz-convergence">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="viz-convergence-svg">
          {objPath && <path d={objPath} className="viz-line-objective" />}
          {boundPath && <path d={boundPath} className="viz-line-bound" />}
        </svg>
        <div className="subtle viz-convergence-caption">
          {solutions} solution{solutions === 1 ? "" : "s"}
          {lastImproved ? ` · ${lastImproved}` : ""}
        </div>
      </div>

      {/* 3. Department-colored week grid */}
      <div className="viz-grid-wrap">
        <div className="viz-legend">
          {vizInit.depts.map((dept, i) => (
            <span key={dept} className="viz-legend-item">
              <span className="viz-swatch" style={{ background: PALETTE[i % 12] }} />
              <span className="subtle">{dept}</span>
            </span>
          ))}
        </div>
        <div className="viz-grid">
          <div className="viz-grid-row viz-grid-header">
            <div className="viz-grid-timecol" />
            {DAYS.map((d) => (
              <div key={d} className="viz-grid-daycol viz-grid-daylabel">
                {DAY_LABEL[d]}
              </div>
            ))}
          </div>
          <div className="viz-grid-rows">
            {rows.map((row) => (
              <div key={row.key} className="viz-grid-row">
                <div className="viz-grid-timecol mono subtle">
                  {row.start}–{row.end}
                </div>
                {DAYS.map((d) => {
                  const slotId = row.slotForDay.get(d);
                  const tiles = slotId != null ? placementsBySlot.get(slotId) ?? [] : [];
                  const flashing = slotId != null && flashSlots.has(slotId);
                  return (
                    <div key={d} className="viz-grid-daycol viz-grid-cell">
                      {slotId != null && (
                        <div className={`viz-tiles${flashing ? " viz-tiles-flash" : ""}`}>
                          {tiles.map((tile, i) => (
                            <span
                              key={i}
                              className="viz-tile"
                              title={tile.label}
                              style={{ background: PALETTE[tile.deptIdx % 12] }}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}
            {gridPlaceholder && <div className="viz-grid-placeholder">{gridPlaceholder}</div>}
          </div>
        </div>
      </div>

      {/* 4. Activity footer */}
      <div className="viz-footer subtle mono">
        {[
          `solutions ${solutions}`,
          moved != null ? `moved ${moved}` : null,
          conflicts != null ? `conflicts ${conflicts}` : null,
          branches != null ? `branches ${branches}` : null,
          restarts != null ? `restarts ${restarts}` : null,
          `elapsed ${fmtMSS(t)}`,
        ]
          .filter((s): s is string => s != null)
          .join(" · ")}
      </div>

      {/* 5. Machine room: proof something is still happening */}
      <div className="viz-machine">
        <div className="viz-liveness mono">
          <span className={`viz-spinner${stale ? " viz-spinner-stale" : ""}`} aria-hidden="true">
            {SPINNER_FRAMES[tick % SPINNER_FRAMES.length]}
          </span>
          {stale ? (
            <span className="viz-stale">no signal for {Math.floor(staleMs / 1000)}s</span>
          ) : (
            <span className="subtle">stream live</span>
          )}
          <span className="subtle">solver output: {logLines.toLocaleString()} lines</span>
        </div>
        <div className="viz-console" aria-label="solver output">
          <div className="viz-console-label subtle">solver output</div>
          {consoleLines.map((cl, i) => (
            <div
              key={cl.tick}
              className="viz-console-line mono"
              style={{ opacity: 0.35 + (0.65 * (i + 1)) / consoleLines.length }}
            >
              {cl.line}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
