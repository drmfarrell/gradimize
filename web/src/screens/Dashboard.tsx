import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { sortNewestFirst, fmtTimestamp } from "../format";
import { useAsync } from "../useAsync";
import { Spinner, ErrorBox, Empty, StatusPill } from "../components";
import { GenerateSchedule } from "../GenerateSchedule";

function AboutPanel() {
  return (
    <div
      className="card"
      style={{ margin: "8px 0 16px", maxWidth: 760, fontSize: 14, lineHeight: 1.5 }}
    >
      <strong>About Gradimize</strong>
      <p style={{ margin: "8px 0 0" }}>
        Gradimize is an academic scheduling engine that takes a course catalog, projected
        student demand (from DegreeWorks degree audits), faculty, and room inventory, and
        uses Google OR-Tools&rsquo; CP-SAT constraint solver to produce a conflict-minimizing
        class schedule — assigning every section a time, room, and instructor so that
        courses the same students are likely to need don&rsquo;t collide.
      </p>
      <p style={{ margin: "8px 0 0" }}>
        Gradimize builds the schedule; <strong>students register themselves</strong>. After
        registration, the actual enrollment data can be imported so Gradimize can also build
        the finals-exam schedule around real enrollments.
      </p>
      <p style={{ margin: "8px 0 0" }}>
        The backend is a Python <strong>FastAPI</strong> service that drives the solver and
        persists everything through <strong>SQLModel</strong> to a database, while a{" "}
        <strong>React/TypeScript</strong> single-page app provides the interface for
        generating, editing, and reviewing schedules. Hard rules (no room or faculty
        double-booking, room-type and capacity fits) are enforced as constraints, while
        preferences (time-of-day, spread, and avoiding likely student course conflicts) are
        weighted objectives the solver minimizes.
      </p>
      <p style={{ margin: "8px 0 0" }} className="subtle">
        Note: this is a single-user beta — one person should edit a schedule at a time.
        Others can browse freely, but simultaneous edits aren&rsquo;t synced and can overwrite
        each other.
      </p>
    </div>
  );
}

const STAT_FIELDS: { key: keyof import("../api").SystemStats; label: string }[] = [
  { key: "total_students", label: "Students" },
  { key: "total_courses", label: "Courses" },
  { key: "total_faculty", label: "Faculty" },
  { key: "total_rooms", label: "Rooms" },
  { key: "total_time_slots", label: "Time Slots" },
  { key: "total_demands", label: "Demands" },
  { key: "schedules_count", label: "Schedules" },
];

export function Dashboard() {
  const stats = useAsync(useCallback(() => api.stats(), []), []);
  const schedules = useAsync(useCallback(() => api.listSchedules(), []), []);
  const [aboutOpen, setAboutOpen] = useState(false);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <h1 style={{ margin: 0 }}>Dashboard</h1>
        <button
          className="ghost"
          onClick={() => setAboutOpen((o) => !o)}
          aria-expanded={aboutOpen}
          style={{ fontSize: 13, padding: "4px 10px" }}
        >
          {aboutOpen ? "Hide" : "ⓘ About"}
        </button>
      </div>
      <p className="subtle">System overview and recent schedules.</p>

      {aboutOpen && <AboutPanel />}

      <GenerateSchedule onCreated={() => { stats.reload(); schedules.reload(); }} />

      <h2>System Statistics</h2>
      {stats.loading && <Spinner />}
      {stats.error && <ErrorBox message={stats.error} />}
      {stats.data && (
        <div className="stat-grid">
          {STAT_FIELDS.map((f) => (
            <div className="stat" key={f.key}>
              <div className="stat-value">{stats.data![f.key]}</div>
              <div className="stat-label">{f.label}</div>
            </div>
          ))}
        </div>
      )}

      <h2>Recent Schedules</h2>
      {schedules.loading && <Spinner />}
      {schedules.error && <ErrorBox message={schedules.error} />}
      {schedules.data && schedules.data.length === 0 && (
        <Empty>No schedules yet. Generate one from the TUI or API.</Empty>
      )}
      {schedules.data && schedules.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Semester</th>
                <th>Status</th>
                <th>Sections</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sortNewestFirst(schedules.data)
                .map((s) => (
                  <tr key={s.id}>
                    <td>{s.name}</td>
                    <td className="mono">{s.semester}</td>
                    <td>
                      <StatusPill status={s.status} />
                    </td>
                    <td>{s.sections_count}</td>
                    <td className="subtle">{fmtTimestamp(s.created_at)}</td>
                    <td>
                      <Link className="btn ghost" to={`/schedules/${s.id}`}>
                        View
                      </Link>{" "}
                      <Link className="btn ghost" to={`/finals/${s.id}`}>
                        Finals
                      </Link>
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
