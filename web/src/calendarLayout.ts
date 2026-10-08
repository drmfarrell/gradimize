// Pure layout logic for the calendar view — no React, no DOM. Extracted from
// CalendarView.tsx so the tricky bits (greedy lane assignment, the day-dominant
// snap rule) are testable in isolation and the component is just rendering.
import type { SectionDetail, TimeSlotInfo } from "./api";
import { toMin } from "./format";

export const DAY_ORDER = ["M", "T", "W", "R", "F", "S", "U"];

const PALETTE = [
  "#4f8cff", "#3fb950", "#d29922", "#bc6bd6", "#e06c9f", "#46b3a3",
  "#e8814a", "#7c8cf0", "#5aa9e6", "#c9a227", "#9b7bd4", "#56b870",
];

function subjectOf(code: string): string {
  const m = code.match(/^[A-Za-z]+/);
  return m ? m[0] : code;
}

function colorFor(subject: string): string {
  let h = 0;
  for (const c of subject) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

// Snap a (day, minute) drop point to the nearest valid slot of the same
// type. Day membership DOMINATES: while any slot of this type includes the
// hovered day, a different-day slot can never win — otherwise the preview
// (drawn in the hovered column) would lie about where the class lands.
// Other-day slots are only a fallback when the type doesn't meet on that
// day at all (e.g. dragging a TR lecture into the Monday column). Returns
// null if no slot of that type exists.
export function snapSlot(
  day: string,
  minutes: number,
  slotType: string,
  slotsByType: Map<string, TimeSlotInfo[]>
): TimeSlotInfo | null {
  const cands = slotsByType.get(slotType);
  if (!cands || cands.length === 0) return null;
  let best: TimeSlotInfo | null = null;
  let bestT = Infinity;
  let bestDay = 2;
  for (const s of cands) {
    const t = Math.abs(toMin(s.start_time) - minutes);
    const d = s.days.includes(day) ? 0 : 1;
    if (d < bestDay || (d === bestDay && t < bestT)) {
      best = s;
      bestT = t;
      bestDay = d;
    }
  }
  return best;
}

export interface Ev {
  key: string;
  sectionId: number;
  day: string;
  startMin: number;
  endMin: number;
  course: string;
  section: string;
  room: string;
  roomId: string;
  slotId: number;
  slotType: string;
  multiMeeting: boolean;
  faculty: string | null;
  enrolled: number;
  capacity: number;
  pinned: boolean;
  color: string;
  subject: string;
}

export type PlacedEv = Ev & { col: number; cols: number };

function section_meeting_count(s: SectionDetail): number {
  return s.meetings.length > 0 ? s.meetings.length : s.time_slot_id != null ? 1 : 0;
}

export function buildEvents(
  sections: SectionDetail[],
  slotsById: Map<number, TimeSlotInfo>
): Ev[] {
  const evs: Ev[] = [];
  for (const s of sections) {
    const meetings =
      s.meetings.length > 0
        ? s.meetings
        : s.time_slot_id != null
        ? [
            {
              meeting_sequence: 0,
              time_slot_id: s.time_slot_id,
              time_slot_name: s.time_slot_name ?? "",
              room_id: s.room_id ?? "",
              room_name: s.room_name ?? "",
            },
          ]
        : [];
    const multiMeeting = section_meeting_count(s) > 1;
    for (const m of meetings) {
      const slot = slotsById.get(m.time_slot_id);
      if (!slot) continue;
      const startMin = toMin(slot.start_time);
      const endMin = toMin(slot.end_time);
      const subject = subjectOf(s.course_code);
      const color = colorFor(subject);
      for (const day of slot.days) {
        evs.push({
          key: `${s.id}-${m.meeting_sequence}-${day}`,
          sectionId: s.id,
          day,
          startMin,
          endMin,
          course: s.course_code,
          section: s.section_number,
          room: m.room_name || m.room_id || "",
          roomId: m.room_id || "",
          slotId: slot.id,
          slotType: slot.slot_type,
          multiMeeting,
          faculty: s.faculty_name,
          enrolled: s.enrollment_count,
          capacity: s.capacity,
          pinned: s.is_pinned,
          color,
          subject,
        });
      }
    }
  }
  return evs;
}

// Greedy interval-graph layout: within each cluster of transitively
// overlapping events, assign each to the first free lane. All events in a
// cluster share the same column count so widths line up.
export function layoutDay(evs: Ev[]): PlacedEv[] {
  const sorted = [...evs].sort(
    (a, b) => a.startMin - b.startMin || a.endMin - b.endMin
  );
  const out: PlacedEv[] = [];
  let cluster: Ev[] = [];
  let clusterEnd = -1;

  const flush = () => {
    const laneEnd: number[] = [];
    const placed: { ev: Ev; col: number }[] = [];
    for (const ev of cluster) {
      let lane = 0;
      while (lane < laneEnd.length && laneEnd[lane] > ev.startMin) lane++;
      laneEnd[lane] = ev.endMin;
      placed.push({ ev, col: lane });
    }
    const cols = laneEnd.length;
    for (const p of placed) out.push({ ...p.ev, col: p.col, cols });
    cluster = [];
    clusterEnd = -1;
  };

  for (const ev of sorted) {
    if (cluster.length && ev.startMin >= clusterEnd) flush();
    cluster.push(ev);
    clusterEnd = Math.max(clusterEnd, ev.endMin);
  }
  if (cluster.length) flush();
  return out;
}
