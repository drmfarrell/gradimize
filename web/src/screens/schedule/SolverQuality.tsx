import type { ScheduleDetail } from "../../api";

// Optimality-gap badge shown next to a schedule's header (table + calendar).
export function SolverQuality({ detail }: { detail: ScheduleDetail }) {
  const gap = (detail.solver_stats as Record<string, unknown>)?.optimality_gap_pct;
  if (typeof gap !== "number") return null;
  return (
    <span
      className="subtle"
      title="How far this schedule could be from the mathematically best one. 0% = proven optimal."
    >
      {gap === 0 ? "✓ proven optimal" : `within ${gap}% of optimal`}
    </span>
  );
}
