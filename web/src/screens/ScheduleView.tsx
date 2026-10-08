import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, fileUrl } from "../api";
import { sortNewestFirst } from "../format";
import type {
  CheckConflictsResponse,
  ScheduleDiff,
  SectionDetail,
  StudentQualityReport,
  ViabilityReport,
} from "../api";
import { useAsync } from "../useAsync";
import { Spinner, ErrorBox, Empty } from "../components";
import { GenerateSchedule } from "../GenerateSchedule";
import { SectionDialog } from "../SectionDialog";
import type { SectionEdit } from "../SectionDialog";
import { ConflictPanel } from "../ConflictPanel";
import { DiffPanel } from "./schedule/DiffPanel";
import { QualityCard } from "./schedule/QualityCard";
import { ReSolvePanel } from "./schedule/ReSolvePanel";
import { ScheduleCalendar } from "./schedule/ScheduleCalendar";
import { ScheduleDetailTable } from "./schedule/ScheduleDetailTable";
import { ViabilityCard } from "./schedule/ViabilityCard";

export function ScheduleView() {
  const { scheduleId } = useParams();
  const navigate = useNavigate();

  const schedules = useAsync(useCallback(() => api.listSchedules(), []), []);
  const timeSlots = useAsync(useCallback(() => api.listTimeSlots(), []), []);
  const faculty = useAsync(useCallback(() => api.listFaculty(), []), []);
  const rooms = useAsync(useCallback(() => api.listRooms(), []), []);

  // View mode is reflected in the URL (?view=calendar) so links are shareable.
  const [searchParams, setSearchParams] = useSearchParams();
  const view: "table" | "calendar" =
    searchParams.get("view") === "calendar" ? "calendar" : "table";
  const setView = (v: "table" | "calendar") => {
    const next = new URLSearchParams(searchParams);
    if (v === "calendar") next.set("view", "calendar");
    else next.delete("view");
    setSearchParams(next, { replace: true });
  };
  const initialDay = searchParams.get("day"); // e.g. ?view=calendar&day=T

  // Default to the most recent schedule when none is in the URL.
  const selectedId = useMemo(() => {
    if (scheduleId) return Number(scheduleId);
    if (schedules.data && schedules.data.length > 0) {
      return sortNewestFirst(schedules.data)[0].id;
    }
    return null;
  }, [scheduleId, schedules.data]);

  const detail = useAsync(
    useCallback(
      () => (selectedId == null ? Promise.resolve(null) : api.getSchedule(selectedId)),
      [selectedId]
    ),
    [selectedId]
  );

  // Optimistic pin state, keyed by section id. Cleared when the schedule changes.
  const [pinOverrides, setPinOverrides] = useState<Record<number, boolean>>({});
  const [pinBusy, setPinBusy] = useState<Set<number>>(new Set());

  const effectivePinned = useCallback(
    (s: SectionDetail) => pinOverrides[s.id] ?? s.is_pinned,
    [pinOverrides]
  );

  async function togglePin(s: SectionDetail) {
    if (selectedId == null) return;
    const next = !effectivePinned(s);
    setPinOverrides((p) => ({ ...p, [s.id]: next }));
    setPinBusy((b) => new Set(b).add(s.id));
    setActionError(null);
    try {
      if (next) await api.pinSection(selectedId, s.id);
      else await api.unpinSection(selectedId, s.id);
    } catch (e) {
      setPinOverrides((p) => ({ ...p, [s.id]: !next })); // revert on failure
      setActionError(
        `${next ? "Pinning" : "Unpinning"} ${s.course_code} failed: ${(e as Error).message}`
      );
    } finally {
      setPinBusy((b) => {
        const n = new Set(b);
        n.delete(s.id);
        return n;
      });
    }
  }

  const pinnedCount = detail.data
    ? detail.data.sections.filter(effectivePinned).length
    : 0;

  // Section lookup + live pin-state accessor for the calendar.
  const sectionsById = useMemo(
    () => new Map((detail.data?.sections ?? []).map((s) => [s.id, s])),
    [detail.data]
  );
  const isPinnedById = useCallback(
    (id: number) => {
      const s = sectionsById.get(id);
      return s ? effectivePinned(s) : false;
    },
    [sectionsById, effectivePinned]
  );

  // Which section's details dialog is open (opened from the calendar).
  const [dialogSectionId, setDialogSectionId] = useState<number | null>(null);
  const dialogSection = dialogSectionId != null ? sectionsById.get(dialogSectionId) : undefined;

  // Manual-edit + conflict-check state.
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false); // manual edits since last solve/check
  const [checkResult, setCheckResult] = useState<CheckConflictsResponse | null>(null);
  const [checking, setChecking] = useState(false);
  // Mutation failures (edit, move, pin, check) surface here — never silently.
  const [actionError, setActionError] = useState<string | null>(null);
  // Section viability (below-minimum sections + cancellation impact).
  const [viability, setViability] = useState<ViabilityReport | null>(null);
  const [viabilityBusy, setViabilityBusy] = useState(false);
  // Student schedule quality (per-student gaps/marathons/day-spread).
  const [quality, setQuality] = useState<StudentQualityReport | null>(null);
  const [qualityBusy, setQualityBusy] = useState(false);

  // "What changed vs another version?" — trust feature for re-solves.
  const [diffBaseId, setDiffBaseId] = useState<number | null>(null);
  const [diff, setDiff] = useState<ScheduleDiff | null>(null);

  // All of the above resets when the schedule changes. Adjusted during
  // render (React's "resetting state when a prop changes" pattern) rather
  // than in an effect, so stale state never paints even for a frame.
  const [prevSelectedId, setPrevSelectedId] = useState(selectedId);
  if (selectedId !== prevSelectedId) {
    setPrevSelectedId(selectedId);
    setPinOverrides({});
    setDialogSectionId(null);
    setDirty(false);
    setCheckResult(null);
    setActionError(null);
    setViability(null);
    setQuality(null);
    setDiffBaseId(null);
    setDiff(null);
  }

  async function runViability() {
    if (selectedId == null) return;
    setViabilityBusy(true);
    try {
      setViability(await api.scheduleViability(selectedId));
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setViabilityBusy(false);
    }
  }

  async function runQuality() {
    if (selectedId == null) return;
    setQualityBusy(true);
    try {
      setQuality(await api.scheduleStudentQuality(selectedId));
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setQualityBusy(false);
    }
  }

  async function saveEdit(edit: SectionEdit) {
    if (selectedId == null || dialogSectionId == null) return;
    if (Object.keys(edit).length === 0) return;
    setSaving(true);
    setActionError(null);
    try {
      await api.editSection(selectedId, dialogSectionId, edit);
      detail.reload();
      setDirty(true);
      setCheckResult(null); // any prior check is stale now
    } catch (e) {
      setActionError(`Saving section failed: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function moveSection(sectionId: number, newSlotId: number) {
    if (selectedId == null) return;
    const s = sectionsById.get(sectionId);
    if (!s) return;
    // Room may be unknown here; the backend defaults it from the section's
    // existing meeting and 422s if there is nothing to default from.
    const room = s.meetings[0]?.room_id ?? s.room_id ?? undefined;
    setSaving(true);
    setActionError(null);
    try {
      await api.editSection(selectedId, sectionId, {
        time_slot_id: newSlotId,
        ...(room != null ? { room_id: room } : {}),
      });
      detail.reload();
      setDirty(true);
      setCheckResult(null);
    } catch (e) {
      setActionError(`Move failed: ${(e as Error).message}`);
      detail.reload(); // snap the calendar back to server state
    } finally {
      setSaving(false);
    }
  }

  async function runConflictCheck() {
    if (selectedId == null) return;
    setChecking(true);
    setActionError(null);
    try {
      setCheckResult(await api.checkConflicts(selectedId));
      setDirty(false); // the warning banner has served its purpose
    } catch (e) {
      setCheckResult(null);
      setActionError(`Conflict check failed: ${(e as Error).message}`);
    } finally {
      setChecking(false);
    }
  }

  // Fetch the diff whenever a comparison base is selected. Clearing the
  // selection (base -> null) resets `diff` directly in the event handler
  // rather than here, since that isn't a "sync with external data" concern.
  useEffect(() => {
    if (selectedId == null || diffBaseId == null) return;
    let alive = true;
    api
      .diffSchedules(selectedId, diffBaseId)
      .then((d) => alive && setDiff(d))
      .catch((e) => alive && setActionError(`Compare failed: ${(e as Error).message}`));
    return () => {
      alive = false;
    };
  }, [selectedId, diffBaseId]);

  return (
    <div>
      <h1>Schedules</h1>

      <GenerateSchedule
        onCreated={(id) => {
          schedules.reload();
          navigate(`/schedules/${id}`);
        }}
      />

      <div className="row" style={{ marginBottom: 16 }}>
        <div>
          <label htmlFor="sched-select">Schedule</label>
          <select
            id="sched-select"
            value={selectedId ?? ""}
            onChange={(e) => navigate(`/schedules/${e.target.value}`)}
            disabled={!schedules.data || schedules.data.length === 0}
          >
            {schedules.data &&
              sortNewestFirst(schedules.data).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.semester})
                </option>
              ))}
          </select>
        </div>
        <div className="nav">
          <button className={view === "table" ? "" : "ghost"} onClick={() => setView("table")}>
            Table
          </button>
          <button
            className={view === "calendar" ? "" : "ghost"}
            onClick={() => setView("calendar")}
          >
            Calendar
          </button>
        </div>
        <div className="spacer" />
        {selectedId != null && (
          <>
            <button
              className={dirty ? "" : "ghost"}
              onClick={runConflictCheck}
              disabled={checking}
            >
              {checking ? "Checking…" : "Check conflicts"}
            </button>
            <button
              className="ghost"
              onClick={runViability}
              disabled={viabilityBusy}
              title="Sections below minimum enrollment — and who gets stranded if each is cancelled (seat- and time-conflict-aware)"
            >
              {viabilityBusy ? "Analyzing…" : "Section viability"}
            </button>
            <button
              className="ghost"
              onClick={runQuality}
              disabled={qualityBusy}
              title="How livable is each student's week — daily gaps, marathon stretches, and day-on-campus spread"
            >
              {qualityBusy ? "Analyzing…" : "Student quality"}
            </button>
            <a className="btn ghost" href={fileUrl(`/schedule/${selectedId}/export/banner`)}>
              Export Banner CSV
            </a>
            <a className="btn ghost" href={fileUrl(`/schedule/${selectedId}/export/enrollments`)}>
              Export Enrollments
            </a>
            <select
              value={diffBaseId ?? ""}
              onChange={(e) => {
                const next = e.target.value ? Number(e.target.value) : null;
                setDiffBaseId(next);
                if (next == null) setDiff(null);
              }}
              title="Show what changed vs another schedule version"
            >
              <option value="">Compare with…</option>
              {schedules.data &&
                sortNewestFirst(schedules.data)
                  .filter((s) => s.id !== selectedId)
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      vs {s.name} ({s.semester})
                    </option>
                  ))}
            </select>
          </>
        )}
      </div>

      {diff && (
        <DiffPanel
          diff={diff}
          onClose={() => {
            setDiffBaseId(null);
            setDiff(null);
          }}
        />
      )}

      {viability && (
        <ViabilityCard report={viability} onClose={() => setViability(null)} />
      )}

      {quality && <QualityCard report={quality} onClose={() => setQuality(null)} />}

      {actionError && (
        <div className="row" style={{ marginBottom: 12 }}>
          <ErrorBox message={actionError} />
          <button className="ghost" onClick={() => setActionError(null)}>
            Dismiss
          </button>
        </div>
      )}

      {dirty && selectedId != null && (
        <div className="banner banner-err" style={{ background: "rgba(210,153,34,0.1)", borderColor: "rgba(210,153,34,0.3)", color: "var(--yellow)" }}>
          ⚠ You've made manual changes — run <strong>Check conflicts</strong> above, or
          <strong> Re-solve</strong> to re-optimize around them.
        </div>
      )}

      {checkResult && (
        <ConflictPanel result={checkResult} onClose={() => setCheckResult(null)} />
      )}

      {selectedId != null && detail.data && detail.data.sections.length > 0 && (
        <ReSolvePanel
          scheduleId={selectedId}
          pinnedCount={pinnedCount}
          attention={dirty}
          onCreated={(id) => {
            schedules.reload();
            navigate(`/schedules/${id}`);
          }}
        />
      )}

      {schedules.loading && <Spinner />}
      {schedules.error && <ErrorBox message={schedules.error} />}
      {schedules.data && schedules.data.length === 0 && (
        <Empty>No schedules found. Generate one above to get started.</Empty>
      )}

      {detail.loading && <Spinner />}
      {detail.error && <ErrorBox message={detail.error} />}
      {detail.data && view === "table" && (
        <ScheduleDetailTable
          detail={detail.data}
          effectivePinned={effectivePinned}
          pinBusy={pinBusy}
          onTogglePin={togglePin}
          pinnedCount={pinnedCount}
        />
      )}
      {detail.data && view === "calendar" && (
        <ScheduleCalendar
          detail={detail.data}
          timeSlots={timeSlots}
          initialDay={initialDay}
          isPinned={isPinnedById}
          onOpenSection={setDialogSectionId}
          onMove={moveSection}
          pinBusy={pinBusy}
        />
      )}

      {dialogSection && timeSlots.data && (
        <SectionDialog
          section={dialogSection}
          scheduleId={selectedId!}
          timeSlots={timeSlots.data}
          faculty={faculty.data ?? []}
          rooms={rooms.data ?? []}
          pinned={effectivePinned(dialogSection)}
          busy={pinBusy.has(dialogSection.id)}
          saving={saving}
          onTogglePin={() => togglePin(dialogSection)}
          onSave={saveEdit}
          onClose={() => setDialogSectionId(null)}
        />
      )}
    </div>
  );
}
