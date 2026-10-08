import { useCallback, useEffect, useState } from "react";
import type {
  FacultyInfo,
  RoomInfo,
  SectionDetail,
  TimeSlotInfo,
} from "./api";
import { fmtTime } from "./format";
import { api } from "./api";
import { useAsync } from "./useAsync";

export interface SectionEdit {
  faculty_id?: string;
  time_slot_id?: number;
  room_id?: string;
}

// Modal showing a section's details with inline editing (instructor / time /
// room) and pin/unpin. Saving an edit pins the section so a re-solve keeps it.
export function SectionDialog({
  section,
  scheduleId,
  timeSlots,
  faculty,
  rooms,
  pinned,
  busy,
  saving,
  onTogglePin,
  onSave,
  onClose,
}: {
  section: SectionDetail;
  scheduleId: number;
  timeSlots: TimeSlotInfo[];
  faculty: FacultyInfo[];
  rooms: RoomInfo[];
  pinned: boolean;
  busy: boolean;
  saving: boolean;
  onTogglePin: () => void;
  onSave: (edit: SectionEdit) => void;
  onClose: () => void;
}) {
  // "Why is it placed here?" — trust feature; loads once per section.
  const explain = useAsync(
    useCallback(
      () => api.explainSection(scheduleId, section.id),
      [scheduleId, section.id]
    ),
    [scheduleId, section.id]
  );

  const primary = section.meetings[0];
  const multiMeeting = section.meetings.length > 1;

  const [facultyId, setFacultyId] = useState(section.faculty_id ?? "");
  const [slotId, setSlotId] = useState<number | "">(primary?.time_slot_id ?? "");
  const [roomId, setRoomId] = useState(primary?.room_id ?? section.room_id ?? "");

  // Re-sync edit state when the dialog switches to a different section.
  // Adjusted during render (React's "resetting state when a prop changes"
  // pattern) rather than in an effect, so it applies before paint.
  const [prevSectionId, setPrevSectionId] = useState(section.id);
  if (section.id !== prevSectionId) {
    setPrevSectionId(section.id);
    setFacultyId(section.faculty_id ?? "");
    setSlotId(section.meetings[0]?.time_slot_id ?? "");
    setRoomId(section.meetings[0]?.room_id ?? section.room_id ?? "");
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const facultyChanged = facultyId !== (section.faculty_id ?? "");
  const slotChanged = !multiMeeting && slotId !== (primary?.time_slot_id ?? "");
  const roomChanged = !multiMeeting && roomId !== (primary?.room_id ?? "");
  const dirty = facultyChanged || slotChanged || roomChanged;

  // Approved instructors first; include the current one even if not approved.
  const approved = faculty.filter(
    (f) => f.approved_courses.length === 0 || f.approved_courses.includes(section.course_code)
  );
  const facultyOptions = [...approved];
  if (section.faculty_id && !approved.some((f) => f.id === section.faculty_id)) {
    const cur = faculty.find((f) => f.id === section.faculty_id);
    if (cur) facultyOptions.unshift(cur);
  }

  const slotOptions = [...timeSlots].sort(
    (a, b) => a.start_time.localeCompare(b.start_time) || a.pattern.localeCompare(b.pattern)
  );
  const roomOptions = [...rooms].sort((a, b) => a.id.localeCompare(b.id));

  function reset() {
    setFacultyId(section.faculty_id ?? "");
    setSlotId(primary?.time_slot_id ?? "");
    setRoomId(primary?.room_id ?? "");
  }

  function save() {
    const edit: SectionEdit = {};
    if (facultyChanged) edit.faculty_id = facultyId || undefined;
    if (slotChanged && slotId !== "") edit.time_slot_id = Number(slotId);
    if (roomChanged) edit.room_id = roomId || undefined;
    // The pin endpoint rewrites the meeting only when both slot+room are sent,
    // so include both whenever either time or room changed.
    if ((slotChanged || roomChanged) && !multiMeeting) {
      edit.time_slot_id = Number(slotId);
      edit.room_id = roomId;
    }
    onSave(edit);
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>
            {section.course_code} · Section {section.section_number}
          </strong>
          <button className="icon-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <div className="detail-row">
            <div className="detail-label">CRN</div>
            <div className="detail-value mono">{section.crn}</div>
          </div>

          <div className="detail-row">
            <div className="detail-label">Instructor</div>
            <div className="detail-value">
              <select
                value={facultyId}
                onChange={(e) => setFacultyId(e.target.value)}
                style={{ width: "100%" }}
              >
                <option value="">— Unassigned —</option>
                {facultyOptions.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="detail-row">
            <div className="detail-label">Time</div>
            <div className="detail-value">
              {multiMeeting ? (
                <div>
                  {section.meetings.map((m) => {
                    const slot = timeSlots.find((t) => t.id === m.time_slot_id);
                    return (
                      <div key={m.meeting_sequence}>
                        {slot
                          ? `${slot.pattern} ${fmtTime(slot.start_time)}–${fmtTime(slot.end_time)}`
                          : m.time_slot_name}
                      </div>
                    );
                  })}
                  <div className="subtle" style={{ fontSize: 11 }}>
                    Multi-meeting time/room editing isn't supported yet.
                  </div>
                </div>
              ) : (
                <select
                  value={slotId}
                  onChange={(e) => setSlotId(e.target.value === "" ? "" : Number(e.target.value))}
                  style={{ width: "100%" }}
                >
                  {slotOptions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.pattern} {fmtTime(s.start_time)}–{fmtTime(s.end_time)}
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>

          <div className="detail-row">
            <div className="detail-label">Room</div>
            <div className="detail-value">
              {multiMeeting ? (
                <div>
                  {section.meetings.map((m) => (
                    <div key={m.meeting_sequence}>{m.room_name || m.room_id}</div>
                  ))}
                </div>
              ) : (
                <select
                  value={roomId}
                  onChange={(e) => setRoomId(e.target.value)}
                  style={{ width: "100%" }}
                >
                  {roomOptions.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.building} {r.number} (cap {r.capacity})
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>

          <div className="detail-row">
            <div className="detail-label">Enrolled</div>
            <div className="detail-value">
              {section.enrollment_count} / {section.capacity}
            </div>
          </div>

          <div className="detail-row">
            <div className="detail-label">Status</div>
            <div className="detail-value">
              {pinned ? (
                <span style={{ color: "var(--yellow)" }}>📌 Pinned in place</span>
              ) : (
                <span className="subtle">Not pinned — solver may move it</span>
              )}
            </div>
          </div>

          <div style={{ marginTop: 12 }}>
            <div className="subtle" style={{ fontWeight: 600, marginBottom: 4 }}>
              Why is it placed here?
            </div>
            {explain.loading && <span className="subtle">Analyzing…</span>}
            {explain.data && (
              <ul className="subtle" style={{ margin: 0, paddingLeft: 18 }}>
                {explain.data.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="modal-footer">
          <button className="ghost" onClick={onClose}>
            Close
          </button>
          {dirty ? (
            <>
              <button className="ghost" onClick={reset} disabled={saving}>
                Reset
              </button>
              <button onClick={save} disabled={saving}>
                {saving ? "Saving…" : "Save changes"}
              </button>
            </>
          ) : (
            <button onClick={onTogglePin} disabled={busy}>
              {busy ? "Saving…" : pinned ? "📌 Unpin" : "📍 Pin in place"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
