import { useEffect, useRef } from "react";
import type { SolverStream } from "./useSolverStream";

// Shared live-log panel for solver runs (generate / re-solve).
export function SolverLog({ stream, doneNote }: { stream: SolverStream; doneNote?: string }) {
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [stream.lines]);

  if (stream.lines.length === 0 && !stream.running) return null;

  return (
    <div>
      <div
        ref={logRef}
        className="mono"
        style={{
          background: "#0b0f16",
          border: "1px solid var(--border)",
          borderRadius: 8,
          padding: 10,
          height: 240,
          overflowY: "auto",
          fontSize: 12,
          lineHeight: 1.5,
          whiteSpace: "pre-wrap",
          marginTop: 10,
        }}
      >
        {stream.lines.map((l, i) => (
          <div key={i}>{l}</div>
        ))}
        {stream.running && <div className="subtle">▌ solving…</div>}
      </div>

      {stream.result && stream.result.schedule_id != null && (
        <div className="banner banner-ok" style={{ marginTop: 10 }}>
          {doneNote ?? `Created schedule #${stream.result.schedule_id}.`}
        </div>
      )}
      {stream.error && (
        <div className="banner banner-err" style={{ marginTop: 10 }}>
          {stream.error}
        </div>
      )}
    </div>
  );
}
