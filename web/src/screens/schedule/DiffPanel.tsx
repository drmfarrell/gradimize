import type { ScheduleDiff } from "../../api";

// "What changed vs another schedule version?" table — a trust feature for
// re-solves. Pure presentational: given a computed diff, render it.
export function DiffPanel({ diff, onClose }: { diff: ScheduleDiff; onClose: () => void }) {
  const total = diff.added.length + diff.removed.length + diff.changed.length;
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="row">
        <h3 style={{ margin: 0 }}>
          Changes vs “{diff.base.name}”
        </h3>
        <span className="subtle">
          {total} section{total === 1 ? "" : "s"} differ · {diff.unchanged_count} unchanged
        </span>
        <div className="spacer" />
        <button className="ghost" onClick={onClose}>
          Close
        </button>
      </div>
      {total === 0 ? (
        <div className="subtle" style={{ marginTop: 8 }}>
          The two versions place every section identically.
        </div>
      ) : (
        <div className="table-wrap" style={{ marginTop: 8, maxHeight: "40vh", overflow: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Section</th>
                <th>Change</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {diff.changed.map((c) => (
                <tr key={`c-${c.course_code}-${c.section_number}`}>
                  <td>
                    {c.course_code}-{c.section_number}
                    {c.pinned ? " 📌" : ""}
                  </td>
                  <td>Moved</td>
                  <td>
                    {Object.entries(c.changes ?? {}).map(([field, d]) => (
                      <div key={field}>
                        {field}: {d.from ?? "—"} → <strong>{d.to ?? "—"}</strong>
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
              {diff.added.map((a) => (
                <tr key={`a-${a.course_code}-${a.section_number}`}>
                  <td>
                    {a.course_code}-{a.section_number}
                  </td>
                  <td>Added</td>
                  <td>
                    {a.slot ?? "unscheduled"}
                    {a.room ? ` · ${a.room}` : ""}
                  </td>
                </tr>
              ))}
              {diff.removed.map((r) => (
                <tr key={`r-${r.course_code}-${r.section_number}`}>
                  <td>
                    {r.course_code}-{r.section_number}
                  </td>
                  <td>Removed</td>
                  <td>
                    {r.slot ?? "unscheduled"}
                    {r.room ? ` · ${r.room}` : ""}
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
