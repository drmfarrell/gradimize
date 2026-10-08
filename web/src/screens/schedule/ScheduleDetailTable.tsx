import type { ScheduleDetail, SectionDetail } from "../../api";
import { Empty, StatusPill } from "../../components";
import { SolverQuality } from "./SolverQuality";

function formatMeetings(s: SectionDetail): string {
  if (s.meetings.length > 0) {
    return s.meetings.map((m) => m.time_slot_name).join(" + ");
  }
  return s.time_slot_name ?? "TBA";
}

function formatRooms(s: SectionDetail): string {
  if (s.meetings.length > 0) {
    return s.meetings.map((m) => m.room_name).join(" + ");
  }
  return s.room_name ?? "TBA";
}

// Sortable section table with an inline pin toggle per row.
export function ScheduleDetailTable({
  detail,
  effectivePinned,
  pinBusy,
  onTogglePin,
  pinnedCount,
}: {
  detail: ScheduleDetail;
  effectivePinned: (s: SectionDetail) => boolean;
  pinBusy: Set<number>;
  onTogglePin: (s: SectionDetail) => void;
  pinnedCount: number;
}) {
  const sections = detail.sections
    .slice()
    .sort(
      (a, b) =>
        a.course_code.localeCompare(b.course_code) ||
        a.section_number.localeCompare(b.section_number)
    );

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>{detail.name}</h2>
        <StatusPill status={detail.status} />
        <span className="subtle mono">{detail.semester}</span>
        <span className="subtle">{detail.sections.length} sections</span>
        {pinnedCount > 0 && <span className="subtle">📌 {pinnedCount} pinned</span>}
        <SolverQuality detail={detail} />
      </div>

      {sections.length === 0 ? (
        <Empty>This schedule has no sections.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>CRN</th>
                <th>Course</th>
                <th>Sec</th>
                <th>Time</th>
                <th>Room</th>
                <th>Instructor</th>
                <th>Enrolled</th>
                <th>Pin</th>
              </tr>
            </thead>
            <tbody>
              {sections.map((s) => {
                const pinned = effectivePinned(s);
                const busy = pinBusy.has(s.id);
                return (
                  <tr key={s.id} className={pinned ? "selected" : undefined}>
                    <td className="mono">{s.crn}</td>
                    <td>{s.course_code}</td>
                    <td>{s.section_number}</td>
                    <td>{formatMeetings(s)}</td>
                    <td>{formatRooms(s)}</td>
                    <td>{s.faculty_name ?? <span className="subtle">TBA</span>}</td>
                    <td>
                      {s.enrollment_count}/{s.capacity}
                    </td>
                    <td>
                      <button
                        className="ghost"
                        title={pinned ? "Pinned — click to unpin" : "Click to pin in place"}
                        disabled={busy}
                        onClick={() => onTogglePin(s)}
                        style={{ padding: "2px 8px", opacity: pinned ? 1 : 0.55 }}
                      >
                        {busy ? "…" : pinned ? "📌 Pinned" : "📍 Pin"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
