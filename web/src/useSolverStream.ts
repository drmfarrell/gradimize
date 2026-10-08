import { useEffect, useRef, useState } from "react";
import { api } from "./api";

export interface RepairDiffChange {
  course_code: string;
  section_number: string;
  pinned: boolean;
  changes: Record<string, { from: string | null; to: string | null }>;
}

// Present on the done event of a repair run: what moved vs the base schedule.
export interface RepairDiff {
  schedule: { id: number; name: string };
  base: { id: number; name: string };
  added: Array<{ course_code: string; section_number: string }>;
  removed: Array<{ course_code: string; section_number: string }>;
  changed: RepairDiffChange[];
  unchanged_count: number;
}

export interface SolverDone {
  type: "done";
  schedule_id: number | null;
  status: string;
  sections_count: number;
  enrollments_count: number;
  objective_value: number;
  solve_time_seconds: number;
  repair_diff?: RepairDiff | null;
}

// Live solver visualization events. Optional feature: absent entirely when
// the server runs with --no-viz, in which case the log UI is unaffected.
export interface VizSlot {
  id: number;
  name: string;
  pattern: string;
  days: string[];
  start: string;
  end: string;
}

export interface VizInitEvent {
  type: "viz_init";
  slots: VizSlot[];
  depts: string[];
  total_sections: number;
  time_limit: number;
}

export interface PhaseEvent {
  type: "phase";
  phase: "presolve" | "searching" | "first_solution" | "improving" | "certifying" | "done";
  t: number;
  status?: string;
  solutions?: number;
  objective?: number;
  gap_pct?: number;
  workers?: number | null;
}

export interface IncumbentEvent {
  type: "incumbent";
  n: number;
  objective: number;
  bound: number;
  gap_pct: number;
  t: number;
  conflicts?: number;
  branches?: number;
  placed?: [number, number, string][];
  moved?: number;
  placed_count?: number;
}

export interface BoundEvent {
  type: "bound";
  bound: number;
  t: number;
  objective?: number;
  gap_pct?: number;
}

export interface ActivityEvent {
  type: "activity";
  t: number;
  phase: string;
  solutions: number;
  quiet_for: number | null;
  objective: number | null;
  bound: number | null;
  conflicts: number | null;
  branches: number | null;
  restarts: number | null;
  log_lines: number;
  gap_pct?: number;
  tick?: number;
  last_log?: string | null;
}

// Raw CP-SAT log line, throttled server-side to ~1/0.3s — the "machine
// room" tail so the viz doesn't look frozen during quiet proving stretches.
export interface SolverLineEvent {
  type: "solver_line";
  line: string;
  tick: number;
  log_lines: number;
  t: number;
}

export type VizEvent =
  | VizInitEvent
  | PhaseEvent
  | IncumbentEvent
  | BoundEvent
  | ActivityEvent
  | SolverLineEvent;

type StreamEvent =
  | { type: "log"; msg: string }
  | SolverDone
  | { type: "error"; message: string }
  | VizEvent;

export interface SolverStream {
  running: boolean;
  lines: string[];
  result: SolverDone | null;
  error: string | null;
  vizEvents: VizEvent[];
  start: (url: string, onDone?: (d: SolverDone) => void) => void;
  stop: () => void;
}

// Drives a solver run over Server-Sent Events: opens an EventSource to
// `url`, collects timestamped log lines, and resolves with a terminal
// done/error event. Closes the stream on the terminal event so the
// browser's EventSource does NOT auto-reconnect (which would re-trigger
// the solve).
//
// Robustness: EventSource can't read HTTP error bodies, so a 409 ("solve
// already running") is indistinguishable from a network drop. start()
// therefore pre-flights the optimization status. And if the connection
// drops MID-solve, the solve continues server-side — we fall back to
// polling the status endpoint and navigate to the new schedule when it
// appears, instead of losing the result.
export function useSolverStream(): SolverStream {
  const [running, setRunning] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [result, setResult] = useState<SolverDone | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [vizEvents, setVizEvents] = useState<VizEvent[]>([]);

  const esRef = useRef<EventSource | null>(null);
  const startedAt = useRef(0);
  const runningRef = useRef(false);
  const recoveryTimer = useRef<number | null>(null);
  const maxIdBefore = useRef(0);

  const clearRecovery = () => {
    if (recoveryTimer.current != null) {
      window.clearInterval(recoveryTimer.current);
      recoveryTimer.current = null;
    }
  };

  useEffect(
    () => () => {
      esRef.current?.close();
      clearRecovery();
    },
    []
  );

  function append(msg: string) {
    const elapsed = ((Date.now() - startedAt.current) / 1000).toFixed(0);
    setLines((prev) => [...prev, `[${elapsed}s] ${msg}`]);
  }

  async function start(url: string, onDone?: (d: SolverDone) => void) {
    esRef.current?.close();
    clearRecovery();
    setLines([]);
    setResult(null);
    setError(null);
    setVizEvents([]);
    setRunning(true);
    runningRef.current = true;
    startedAt.current = Date.now();

    // Pre-flight: give a real message for the 409 case, and remember the
    // newest existing schedule id so recovery can spot the one this run adds.
    try {
      const [status, schedules] = await Promise.all([
        api.optimizationStatus(),
        api.listSchedules(),
      ]);
      if (status.running) {
        setError(
          "An optimization is already running — wait for it to finish before starting another."
        );
        setRunning(false);
        runningRef.current = false;
        return;
      }
      maxIdBefore.current = Math.max(0, ...schedules.map((s) => s.id));
    } catch {
      maxIdBefore.current = 0; // best effort — still try to stream
    }
    if (!runningRef.current) return; // stopped during pre-flight

    const es = new EventSource(url);
    esRef.current = es;

    es.onmessage = (e) => {
      let data: StreamEvent;
      try {
        data = JSON.parse(e.data);
      } catch {
        return;
      }
      if (data.type === "log") {
        append(data.msg);
      } else if (data.type === "done") {
        append(
          `DONE: ${data.status} — ${data.sections_count} sections, ` +
            `${data.enrollments_count} enrollments, obj ${data.objective_value.toFixed(0)}, ` +
            `${data.solve_time_seconds.toFixed(1)}s`
        );
        setResult(data);
        setRunning(false);
        runningRef.current = false;
        es.close();
        if (data.schedule_id != null) onDone?.(data);
      } else if (data.type === "error") {
        append(`ERROR: ${data.message}`);
        setError(data.message);
        setRunning(false);
        runningRef.current = false;
        es.close();
      } else if (
        data.type === "viz_init" ||
        data.type === "phase" ||
        data.type === "incumbent" ||
        data.type === "bound" ||
        data.type === "activity" ||
        data.type === "solver_line"
      ) {
        setVizEvents((prev) => [...prev, data]);
      }
    };

    es.onerror = () => {
      es.close();
      if (esRef.current !== es || !runningRef.current) return;
      // The solve continues server-side; poll for its completion instead of
      // hard-failing and losing the result.
      append("Connection lost — waiting for the solver to finish…");
      const deadline = Date.now() + 30 * 60 * 1000;
      recoveryTimer.current = window.setInterval(async () => {
        if (Date.now() > deadline) {
          clearRecovery();
          setError("Solver stream was lost and recovery timed out — check the schedule list.");
          setRunning(false);
          runningRef.current = false;
          return;
        }
        try {
          const status = await api.optimizationStatus();
          if (status.running) return; // still solving
          clearRecovery();
          const schedules = await api.listSchedules();
          const newest = schedules
            .filter((s) => s.id > maxIdBefore.current)
            .sort((a, b) => b.id - a.id)[0];
          setRunning(false);
          runningRef.current = false;
          if (newest) {
            append(`Recovered: solver finished — opening schedule #${newest.id}.`);
            onDone?.({
              type: "done",
              schedule_id: newest.id,
              status: "recovered",
              sections_count: newest.sections_count,
              enrollments_count: 0,
              objective_value: 0,
              solve_time_seconds: (Date.now() - startedAt.current) / 1000,
            });
          } else {
            setError(
              "Solver stream was lost and no new schedule appeared — the run may have failed. Check the schedule list."
            );
          }
        } catch {
          // transient poll error — keep trying until the deadline
        }
      }, 3000);
    };
  }

  function stop() {
    esRef.current?.close();
    clearRecovery();
    setRunning(false);
    runningRef.current = false;
    append("Stopped watching (server solve continues in background).");
  }

  return { running, lines, result, error, vizEvents, start, stop };
}
