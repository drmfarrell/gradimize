import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { SectionDetail, TimeSlotInfo } from "./api";
import {
  buildEvents,
  DAY_ORDER,
  layoutDay,
  snapSlot,
  type PlacedEv,
} from "./calendarLayout";
import { EventBlock } from "./EventBlock";
import { fmtTimeMin as fmtTime, toMin } from "./format";

const DAY_LABEL: Record<string, string> = {
  M: "Mon",
  T: "Tue",
  W: "Wed",
  R: "Thu",
  F: "Fri",
  S: "Sat",
  U: "Sun",
};
const DEFAULT_PX_PER_MIN = 0.9;
const ZOOM_KEY = "gradimize.calendar.pxPerMin";

export function CalendarView({
  sections,
  timeSlots,
  initialMode = "week",
  initialDay = "M",
  isPinned,
  onOpenSection,
  onMove,
  pinBusy,
}: {
  sections: SectionDetail[];
  timeSlots: TimeSlotInfo[];
  initialMode?: "week" | "day";
  initialDay?: string;
  // Live pin state (for the indicator) + open-details handler, threaded
  // from the schedule screen. Double-clicking a block opens its dialog.
  isPinned?: (sectionId: number) => boolean;
  onOpenSection?: (sectionId: number) => void;
  onMove?: (sectionId: number, newSlotId: number) => void;
  pinBusy?: Set<number>;
}) {
  const [mode, setMode] = useState<"week" | "day">(initialMode);
  const [day, setDay] = useState(initialDay);

  // Vertical scale (px per minute), user-adjustable and persisted so a
  // chosen zoom sticks across sessions / schedules.
  const [pxPerMin, setPxPerMin] = useState(() => {
    const saved = Number(localStorage.getItem(ZOOM_KEY));
    return saved >= 0.4 && saved <= 4 ? saved : DEFAULT_PX_PER_MIN;
  });
  useEffect(() => {
    localStorage.setItem(ZOOM_KEY, String(pxPerMin));
  }, [pxPerMin]);

  const slotsById = useMemo(
    () => new Map(timeSlots.map((t) => [t.id, t])),
    [timeSlots]
  );
  const events = useMemo(() => buildEvents(sections, slotsById), [sections, slotsById]);

  // Days actually present, in canonical order.
  const presentDays = useMemo(() => {
    const set = new Set(events.map((e) => e.day));
    return DAY_ORDER.filter((d) => set.has(d));
  }, [events]);

  const daysToShow = useMemo(
    () =>
      mode === "week" ? (presentDays.length ? presentDays : ["M", "T", "W", "R", "F"]) : [day],
    [mode, presentDays, day]
  );

  // Vertical time bounds, snapped to the hour.
  const { minMin, maxMin } = useMemo(() => {
    if (events.length === 0) return { minMin: 8 * 60, maxMin: 18 * 60 };
    let lo = Math.min(...events.map((e) => e.startMin));
    let hi = Math.max(...events.map((e) => e.endMin));
    lo = Math.floor(lo / 60) * 60;
    hi = Math.ceil(hi / 60) * 60;
    return { minMin: lo, maxMin: hi };
  }, [events]);

  const height = (maxMin - minMin) * pxPerMin;
  const hours: number[] = [];
  for (let h = minMin; h <= maxMin; h += 60) hours.push(h);

  // Per-day placement.
  const placedByDay = useMemo(() => {
    const map = new Map<string, PlacedEv[]>();
    for (const d of daysToShow) {
      map.set(
        d,
        layoutDay(events.filter((e) => e.day === d))
      );
    }
    return map;
  }, [events, daysToShow]);

  const subjects = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of events) if (!m.has(e.subject)) m.set(e.subject, e.color);
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [events]);

  const slotsByType = useMemo(() => {
    const m = new Map<string, TimeSlotInfo[]>();
    for (const t of timeSlots) {
      const arr = m.get(t.slot_type) ?? [];
      arr.push(t);
      m.set(t.slot_type, arr);
    }
    return m;
  }, [timeSlots]);

  // ── Drag-and-drop to move a class to a different time slot ──────────
  // Pointer-based so we can show a live snap preview. dragRef holds the
  // in-flight drag; preview is the highlighted target slot.
  const gridRefs = useRef(new Map<string, HTMLDivElement>());
  const dragRef = useRef<{
    sectionId: number;
    slotType: string;
    fromSlotId: number;
    startX: number;
    startY: number;
    active: boolean;
    label: string;
    color: string;
    durationMin: number;
  } | null>(null);
  const [preview, setPreview] = useState<{
    days: string[]; // the snapped slot's actual days — preview renders in ALL of them
    slotId: number;
    startMin: number;
    endMin: number;
    label: string;
    color: string;
    isMove: boolean;
  } | null>(null);
  const previewRef = useRef(preview);
  useEffect(() => {
    previewRef.current = preview;
  }, [preview]);
  const [draggingId, setDraggingId] = useState<number | null>(null);
  // Block that follows the cursor while dragging.
  const [ghost, setGhost] = useState<{ x: number; y: number; label: string; color: string } | null>(null);
  // Section to briefly flash after a drop, so you see where it landed.
  const [flashId, setFlashId] = useState<number | null>(null);
  const flashTimer = useRef<number | null>(null);
  useEffect(() => () => { if (flashTimer.current) window.clearTimeout(flashTimer.current); }, []);
  // Tear down an in-flight drag's document listeners if we unmount mid-drag.
  const dragCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => { dragCleanupRef.current?.(); }, []);

  function startDrag(e: ReactPointerEvent, ev: PlacedEv) {
    if (!onMove || ev.multiMeeting) return;
    e.preventDefault();
    dragRef.current = {
      sectionId: ev.sectionId,
      slotType: ev.slotType,
      fromSlotId: ev.slotId,
      startX: e.clientX,
      startY: e.clientY,
      active: false,
      label: `${ev.course} ${ev.section}`,
      color: ev.color,
      durationMin: ev.endMin - ev.startMin,
    };
    const onPointerMove = (me: PointerEvent) => {
      const d = dragRef.current;
      if (!d) return;
      if (!d.active) {
        if (Math.hypot(me.clientX - d.startX, me.clientY - d.startY) < 5) return;
        d.active = true;
        setDraggingId(d.sectionId);
      }
      setGhost({ x: me.clientX, y: me.clientY, label: d.label, color: d.color });
      // Which day column is the cursor over?
      let overDay: string | null = null;
      let gridTop = 0;
      for (const [dk, el] of gridRefs.current) {
        const r = el.getBoundingClientRect();
        if (me.clientX >= r.left && me.clientX <= r.right) {
          overDay = dk;
          gridTop = r.top;
          break;
        }
      }
      if (!overDay) {
        setPreview(null);
        return;
      }
      const minutes = minMin + (me.clientY - gridTop) / pxPerMin;
      const slot = snapSlot(overDay, minutes, d.slotType, slotsByType);
      setPreview(
        slot
          ? {
              days: slot.days,
              slotId: slot.id,
              startMin: toMin(slot.start_time),
              endMin: toMin(slot.end_time),
              label: d.label,
              color: d.color,
              isMove: slot.id !== d.fromSlotId,
            }
          : null
      );
    };
    // Shared teardown for drop, cancel (touch gesture / focus loss), and unmount.
    const cancelDrag = () => {
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerup", onPointerUp);
      document.removeEventListener("pointercancel", onPointerCancel);
      dragCleanupRef.current = null;
      dragRef.current = null;
      setDraggingId(null);
      setPreview(null);
      setGhost(null);
    };
    const onPointerCancel = () => cancelDrag();
    const onPointerUp = () => {
      const d = dragRef.current;
      const p = previewRef.current;
      cancelDrag();
      if (d && d.active && p && p.slotId !== d.fromSlotId && onMove) {
        onMove(d.sectionId, p.slotId);
        // Flash the moved section once the new layout renders.
        setFlashId(d.sectionId);
        if (flashTimer.current) window.clearTimeout(flashTimer.current);
        flashTimer.current = window.setTimeout(() => setFlashId(null), 1600);
      }
    };
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerCancel);
    dragCleanupRef.current = cancelDrag;
  }

  if (timeSlots.length === 0) {
    return <div className="state state-empty">Loading time slots…</div>;
  }
  if (events.length === 0) {
    return (
      <div className="state state-empty">
        No placeable meetings — sections may lack time-slot assignments.
      </div>
    );
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <div className="nav">
          <button className={mode === "week" ? "" : "ghost"} onClick={() => setMode("week")}>
            Week
          </button>
          <button className={mode === "day" ? "" : "ghost"} onClick={() => setMode("day")}>
            Single day
          </button>
        </div>
        {mode === "day" && (
          <div className="nav">
            {(presentDays.length ? presentDays : ["M", "T", "W", "R", "F"]).map((d) => (
              <button key={d} className={d === day ? "" : "ghost"} onClick={() => setDay(d)}>
                {DAY_LABEL[d]}
              </button>
            ))}
          </div>
        )}
        <div className="row" style={{ gap: 6 }}>
          <label htmlFor="cal-zoom" title="Vertical scale">
            Height
          </label>
          <button
            className="ghost"
            title="Shorter"
            onClick={() => setPxPerMin((z) => Math.max(0.4, Math.round((z - 0.2) * 10) / 10))}
            style={{ padding: "2px 8px" }}
          >
            −
          </button>
          <input
            id="cal-zoom"
            type="range"
            min={0.4}
            max={4}
            step={0.1}
            value={pxPerMin}
            onChange={(e) => setPxPerMin(Number(e.target.value))}
            style={{ width: 120 }}
          />
          <button
            className="ghost"
            title="Taller"
            onClick={() => setPxPerMin((z) => Math.min(4, Math.round((z + 0.2) * 10) / 10))}
            style={{ padding: "2px 8px" }}
          >
            +
          </button>
        </div>
        <div className="spacer" />
        <div className="row" style={{ gap: 8 }}>
          {subjects.map(([subj, color]) => (
            <span key={subj} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: 2,
                  background: color,
                  display: "inline-block",
                }}
              />
              <span className="subtle" style={{ fontSize: 11 }}>
                {subj}
              </span>
            </span>
          ))}
        </div>
      </div>

      {onOpenSection && (
        <div className="subtle" style={{ fontSize: 11, marginBottom: 6 }}>
          Double-click a class for details and pin/unpin.
          {onMove && " Drag a class to move it to another time slot."} Pinned classes (📌, gold
          outline) stay fixed when you re-solve.
        </div>
      )}

      <div
        className="table-wrap"
        style={{ display: "flex", maxHeight: "88vh", overflow: "auto" }}
      >
        {/* Time gutter */}
        <div style={{ position: "relative", width: 56, flex: "0 0 56px", height: height + 24 }}>
          <div style={{ height: 24 }} />
          {hours.map((h) => (
            <div
              key={h}
              className="subtle mono"
              style={{
                position: "absolute",
                top: 24 + (h - minMin) * pxPerMin - 6,
                right: 6,
                fontSize: 10,
              }}
            >
              {fmtTime(h)}
            </div>
          ))}
        </div>

        {/* Day columns */}
        {daysToShow.map((d) => (
          <div
            key={d}
            style={{
              flex: 1,
              minWidth: mode === "day" ? 320 : 120,
              borderLeft: "1px solid var(--border)",
            }}
          >
            <div
              className="mono"
              style={{
                height: 24,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 12,
                fontWeight: 600,
                background: "var(--panel-2)",
                position: "sticky",
                top: 0,
                zIndex: 1,
              }}
            >
              {DAY_LABEL[d]}
            </div>
            <div
              ref={(el) => {
                if (el) gridRefs.current.set(d, el);
                else gridRefs.current.delete(d);
              }}
              style={{
                position: "relative",
                height,
                backgroundImage:
                  "repeating-linear-gradient(180deg, var(--border) 0, var(--border) 1px, transparent 1px, transparent " +
                  60 * pxPerMin +
                  "px)",
              }}
            >
              {preview && preview.days.includes(d) && (
                <div
                  style={{
                    position: "absolute",
                    top: (preview.startMin - minMin) * pxPerMin,
                    height: Math.max((preview.endMin - preview.startMin) * pxPerMin - 2, 16),
                    left: 4,
                    right: 4,
                    border: "2px dashed var(--accent)",
                    borderRadius: 5,
                    background: `${preview.color}22`,
                    boxShadow: "0 0 0 9999px rgba(0,0,0,0)",
                    pointerEvents: "none",
                    zIndex: 6,
                    fontSize: 10,
                    fontWeight: 700,
                    padding: "2px 5px",
                    color: "var(--accent)",
                    overflow: "hidden",
                  }}
                >
                  {preview.label}
                  <div style={{ fontWeight: 500 }}>
                    → {fmtTime(preview.startMin)}–{fmtTime(preview.endMin)}
                  </div>
                </div>
              )}
              {(placedByDay.get(d) ?? []).map((e) => (
                <EventBlock
                  key={e.key}
                  e={e}
                  minMin={minMin}
                  pxPerMin={pxPerMin}
                  pinned={isPinned ? isPinned(e.sectionId) : e.pinned}
                  busy={pinBusy?.has(e.sectionId) ?? false}
                  dragging={draggingId === e.sectionId}
                  flash={flashId === e.sectionId}
                  canDrag={!!onMove && !e.multiMeeting}
                  onDragStart={(pe) => startDrag(pe, e)}
                  onOpen={onOpenSection ? () => onOpenSection(e.sectionId) : undefined}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      {ghost && (
        <div
          style={{
            position: "fixed",
            left: ghost.x + 12,
            top: ghost.y + 12,
            zIndex: 1000,
            pointerEvents: "none",
            background: `${ghost.color}cc`,
            color: "#0b0f16",
            fontWeight: 700,
            fontSize: 11,
            padding: "3px 8px",
            borderRadius: 5,
            border: "1px solid rgba(255,255,255,0.5)",
            boxShadow: "0 4px 14px rgba(0,0,0,0.5)",
            whiteSpace: "nowrap",
          }}
        >
          {ghost.label}
        </div>
      )}
    </div>
  );
}
