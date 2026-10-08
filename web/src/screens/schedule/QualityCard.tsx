import type { StudentQualityReport } from "../../api";

// Student schedule-quality report: how livable each student's week is —
// daily gaps, marathon stretches, day-on-campus spread — with the worst
// offenders tabled.
export function QualityCard({
  report,
  onClose,
}: {
  report: StudentQualityReport;
  onClose: () => void;
}) {
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="row">
        <strong>
          {report.total_students} student{report.total_students === 1 ? "" : "s"} — mean{" "}
          {report.mean_days_on_campus.toFixed(1)} days on campus, mean gap{" "}
          {(report.mean_gap_minutes / 60).toFixed(1)}h
        </strong>
        <div className="spacer" />
        <button className="ghost" onClick={onClose}>
          Close
        </button>
      </div>
      <div className="subtle" style={{ marginTop: 4 }}>
        {report.students_5_day} with 5-day spreads · {report.students_big_gap} with 3h+
        single gaps · {report.students_heavy_marathon} with 3+ marathons over 3h ·{" "}
        {report.students_no_lunch} with no lunch break · {report.students_long_workday}{" "}
        with 6h+ workdays
      </div>
      {report.worst_students.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead>
              <tr>
                <th>Student</th>
                <th>Days</th>
                <th>Gap (h)</th>
                <th>Marathon (h)</th>
                <th>Earliest</th>
                <th>Latest</th>
              </tr>
            </thead>
            <tbody>
              {report.worst_students.slice(0, 10).map((sq) => (
                <tr key={sq.student_id}>
                  <td className="mono">{sq.student_id}</td>
                  <td>{sq.days_on_campus}</td>
                  <td>{(sq.total_gap_minutes / 60).toFixed(1)}</td>
                  <td>{(sq.max_marathon_minutes / 60).toFixed(1)}</td>
                  <td>{sq.earliest_start ?? "—"}</td>
                  <td>{sq.latest_end ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
