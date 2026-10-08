import type { PointerEvent as ReactPointerEvent } from "react";
import type { PlacedEv } from "./calendarLayout";
import { fmtTimeMin as fmtTime } from "./format";

// One class block in the calendar grid. Purely presentational: absolute
// position/size come from the placed event; drag/open behavior is delegated
// to the parent via callbacks.
export function EventBlock({
  e,
  minMin,
  pxPerMin,
  pinned,
  busy,
  dragging,
  flash,
  canDrag,
  onDragStart,
  onOpen,
}: {
  e: PlacedEv;
  minMin: number;
  pxPerMin: number;
  pinned: boolean;
  busy: boolean;
  dragging: boolean;
  flash: boolean;
  canDrag: boolean;
  onDragStart: (e: ReactPointerEvent) => void;
  onOpen?: () => void;
}) {
  const top = (e.startMin - minMin) * pxPerMin;
  const h = (e.endMin - e.startMin) * pxPerMin;
  const widthPct = 100 / e.cols;
  const leftPct = e.col * widthPct;
  const tall = h >= 34;
  const interactive = !!onOpen;
  const title =
    `${e.course} ${e.section} — ${fmtTime(e.startMin)}–${fmtTime(e.endMin)}\n` +
    `${e.room || "no room"}${e.faculty ? " · " + e.faculty : ""}\n` +
    `${e.enrolled}/${e.capacity} enrolled${pinned ? " · pinned" : ""}` +
    (interactive ? "\n(double-click for details" : "") +
    (interactive && canDrag ? "; drag to move" : interactive ? ")" : "") +
    (interactive && canDrag ? ")" : "");

  return (
    <div
      title={title}
      onDoubleClick={onOpen}
      onPointerDown={canDrag ? onDragStart : undefined}
      style={{
        position: "absolute",
        top,
        height: Math.max(h - 2, 12),
        left: `calc(${leftPct}% + 1px)`,
        width: `calc(${widthPct}% - 2px)`,
        background: `${e.color}26`,
        borderLeft: `3px solid ${e.color}`,
        borderRadius: 4,
        padding: "1px 4px",
        overflow: "hidden",
        fontSize: 10,
        lineHeight: 1.25,
        color: "var(--text)",
        outline: flash
          ? "2px solid var(--accent)"
          : pinned
          ? "1.5px solid var(--yellow)"
          : "none",
        outlineOffset: -1,
        opacity: busy ? 0.5 : dragging ? 0.35 : 1,
        boxShadow: dragging ? "0 0 0 2px var(--accent)" : undefined,
        animation: flash ? "movedFlash 1.6s ease-out" : undefined,
        zIndex: flash ? 8 : undefined,
        cursor: canDrag ? "grab" : interactive ? "pointer" : "default",
        touchAction: canDrag ? "none" : undefined,
        userSelect: "none",
      }}
    >
      <div style={{ fontWeight: 700, whiteSpace: "nowrap", textOverflow: "ellipsis", overflow: "hidden" }}>
        {busy ? "… " : pinned ? "📌 " : ""}
        {e.course} {e.section}
      </div>
      {tall && (
        <div className="subtle" style={{ whiteSpace: "nowrap", textOverflow: "ellipsis", overflow: "hidden" }}>
          {e.room}
        </div>
      )}
    </div>
  );
}
