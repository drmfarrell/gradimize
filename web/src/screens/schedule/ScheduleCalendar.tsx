import type { ScheduleDetail, TimeSlotInfo } from "../../api";
import { CalendarView } from "../../CalendarView";
import { ErrorBox, Spinner, StatusPill } from "../../components";
import type { AsyncState } from "../../useAsync";
import { SolverQuality } from "./SolverQuality";

// Calendar tab: schedule header + the week/day grid. Threads live pin state
// and the move/open handlers down to CalendarView.
export function ScheduleCalendar({
  detail,
  timeSlots,
  initialDay,
  isPinned,
  onOpenSection,
  onMove,
  pinBusy,
}: {
  detail: ScheduleDetail;
  timeSlots: AsyncState<TimeSlotInfo[]>;
  initialDay: string | null;
  isPinned: (sectionId: number) => boolean;
  onOpenSection: (sectionId: number) => void;
  onMove: (sectionId: number, newSlotId: number) => void;
  pinBusy: Set<number>;
}) {
  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>{detail.name}</h2>
        <StatusPill status={detail.status} />
        <span className="subtle mono">{detail.semester}</span>
        <span className="subtle">{detail.sections.length} sections</span>
        <SolverQuality detail={detail} />
      </div>
      {timeSlots.loading && <Spinner />}
      {timeSlots.error && <ErrorBox message={timeSlots.error} />}
      {timeSlots.data && (
        <CalendarView
          sections={detail.sections}
          timeSlots={timeSlots.data}
          initialMode={initialDay ? "day" : "week"}
          initialDay={initialDay ?? "M"}
          key={initialDay ?? "week"}
          isPinned={isPinned}
          onOpenSection={onOpenSection}
          onMove={onMove}
          pinBusy={pinBusy}
        />
      )}
    </div>
  );
}
