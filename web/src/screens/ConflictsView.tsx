import { useCallback, useState } from "react";
import { api } from "../api";
import { useAsync } from "../useAsync";
import { Spinner, ErrorBox, Empty } from "../components";

export function ConflictsView() {
  const [threshold, setThreshold] = useState(50);
  // applied is the value actually queried; threshold is the input box.
  const [applied, setApplied] = useState(50);

  const conflicts = useAsync(
    useCallback(() => api.conflicts(applied, 200), [applied]),
    [applied]
  );

  function maxScore(): number {
    if (!conflicts.data || conflicts.data.high_conflict_pairs.length === 0) return 1;
    return conflicts.data.high_conflict_pairs[0].conflict_score || 1;
  }

  return (
    <div>
      <h1>Conflicts</h1>
      <p className="subtle">
        Course pairs whose enrolled students overlap most — high scores mean the two courses
        should avoid sharing a meeting time.
      </p>

      <div className="row" style={{ marginBottom: 16 }}>
        <label htmlFor="thr">Min conflict score</label>
        <input
          id="thr"
          type="number"
          min={0}
          step={5}
          value={threshold}
          style={{ width: 90 }}
          onChange={(e) => setThreshold(Number(e.target.value))}
        />
        <button onClick={() => setApplied(threshold)}>Apply</button>
        {conflicts.data && (
          <span className="subtle">
            {conflicts.data.high_conflict_pairs.length} pairs over {conflicts.data.threshold} ·{" "}
            {conflicts.data.total_courses} courses
          </span>
        )}
      </div>

      {conflicts.loading && <Spinner />}
      {conflicts.error && <ErrorBox message={conflicts.error} />}
      {conflicts.data && conflicts.data.high_conflict_pairs.length === 0 && (
        <Empty>
          No course pairs above the threshold. Lower it, or import students + demands first.
        </Empty>
      )}
      {conflicts.data && conflicts.data.high_conflict_pairs.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 40 }}>#</th>
                <th>Course A</th>
                <th>Course B</th>
                <th style={{ width: 100 }}>Score</th>
                <th>Relative</th>
              </tr>
            </thead>
            <tbody>
              {conflicts.data.high_conflict_pairs.map((p, i) => (
                <tr key={`${p.course1}-${p.course2}`}>
                  <td className="subtle">{i + 1}</td>
                  <td>{p.course1}</td>
                  <td>{p.course2}</td>
                  <td className="mono">{p.conflict_score.toFixed(1)}</td>
                  <td style={{ width: 200 }}>
                    <div
                      style={{
                        height: 8,
                        borderRadius: 4,
                        background: "var(--accent)",
                        width: `${Math.max(4, (p.conflict_score / maxScore()) * 100)}%`,
                      }}
                    />
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
