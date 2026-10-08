import type { CheckConflictsResponse, ConflictItem } from "./api";

const KIND_STYLE: Record<ConflictItem["kind"], { label: string; color: string }> = {
  room: { label: "ROOM", color: "var(--red)" },
  faculty: { label: "FACULTY", color: "var(--red)" },
  student: { label: "STUDENT", color: "var(--yellow)" },
};

export function ConflictPanel({
  result,
  onClose,
}: {
  result: CheckConflictsResponse;
  onClose: () => void;
}) {
  if (result.total === 0) {
    return (
      <div className="banner banner-ok" style={{ display: "flex", justifyContent: "space-between" }}>
        <span>No conflicts found ✓ — rooms, instructors, and student times are all clear.</span>
        <button className="icon-btn" onClick={onClose}>
          ✕
        </button>
      </div>
    );
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="row" style={{ marginBottom: 8 }}>
        <strong>
          {result.total} conflict{result.total === 1 ? "" : "s"}
        </strong>
        <span className="subtle">
          {result.room} room · {result.faculty} faculty · {result.student} student
        </span>
        <div className="spacer" />
        <button className="icon-btn" onClick={onClose} title="Dismiss">
          ✕
        </button>
      </div>

      {result.room + result.faculty > 0 && (
        <div className="subtle" style={{ fontSize: 11, marginBottom: 6 }}>
          Room and instructor clashes are hard problems (often caused by a manual edit). Student
          overlaps are soft — the solver minimizes but doesn't forbid them.
        </div>
      )}

      <div className="table-wrap" style={{ maxHeight: "40vh", overflow: "auto" }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 90 }}>Type</th>
              <th>Detail</th>
              <th>Sections</th>
            </tr>
          </thead>
          <tbody>
            {result.conflicts.map((c, i) => (
              <tr key={i}>
                <td>
                  <span
                    className="pill"
                    style={{ background: "transparent", color: KIND_STYLE[c.kind].color, border: `1px solid ${KIND_STYLE[c.kind].color}` }}
                  >
                    {KIND_STYLE[c.kind].label}
                  </span>
                </td>
                <td>{c.detail}</td>
                <td>
                  {c.sections.map((s) => `${s.course_code} ${s.section_number}`).join("  ×  ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {result.total > result.conflicts.length && (
        <p className="subtle" style={{ fontSize: 11 }}>
          Showing {result.conflicts.length} of {result.total} (room/faculty first).
        </p>
      )}
    </div>
  );
}
