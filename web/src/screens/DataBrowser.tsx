import { useCallback, useState } from "react";
import { api, ApiError } from "../api";
import type { InferOfferingsResponse } from "../api";
import { useAsync } from "../useAsync";
import { Spinner, ErrorBox, Empty } from "../components";

type Tab = "students" | "courses" | "faculty" | "rooms";
const TABS: { key: Tab; label: string }[] = [
  { key: "students", label: "Students" },
  { key: "courses", label: "Courses" },
  { key: "faculty", label: "Faculty" },
  { key: "rooms", label: "Rooms" },
];

export function DataBrowser() {
  const [tab, setTab] = useState<Tab>("courses");

  return (
    <div>
      <h1>Data Browser</h1>

      <div className="row" style={{ marginBottom: 16 }}>
        <div className="nav">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={t.key === tab ? "" : "ghost"}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === "students" && <StudentsTab />}
      {tab === "courses" && <CoursesTab />}
      {tab === "faculty" && <FacultyTab />}
      {tab === "rooms" && <RoomsTab />}
    </div>
  );
}

function StudentsTab() {
  const q = useAsync(useCallback(() => api.listStudents(500), []), []);
  if (q.loading) return <Spinner />;
  if (q.error) return <ErrorBox message={q.error} />;
  if (!q.data || q.data.length === 0) return <Empty>No students. Import a roster first.</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Name</th>
            <th>Standing</th>
            <th>Major</th>
            <th>Credits</th>
          </tr>
        </thead>
        <tbody>
          {q.data.map((s) => (
            <tr key={s.id}>
              <td className="mono">{s.id}</td>
              <td>{s.name}</td>
              <td>{s.class_standing}</td>
              <td>{s.major}</td>
              <td>{s.credits_earned}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {q.data.length >= 500 && <p className="subtle">Showing first 500.</p>}
    </div>
  );
}

function CoursesTab() {
  const q = useAsync(useCallback(() => api.listCourses(500), []), []);
  const [inferring, setInferring] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function infer() {
    setInferring(true);
    setMsg(null);
    try {
      const r: InferOfferingsResponse = await api.inferOfferings(true);
      let text = `Inferred from ${r.schedules_examined} schedules: ${r.courses_changed} of ${r.courses_with_history} courses with history updated (${r.courses_examined} examined).`;
      if (r.skipped_unparseable_semesters.length > 0)
        text += ` Skipped ${r.skipped_unparseable_semesters.length} unparseable semester codes.`;
      setMsg({ ok: true, text });
      q.reload();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setInferring(false);
    }
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={infer} disabled={inferring}>
          {inferring ? "Inferring…" : "Infer Offerings from History"}
        </button>
        <span className="subtle">
          Sets each course's offer terms & cadence from past schedules.
        </span>
      </div>
      {msg && <div className={msg.ok ? "banner banner-ok" : "banner banner-err"}>{msg.text}</div>}

      {q.loading && <Spinner />}
      {q.error && <ErrorBox message={q.error} />}
      {q.data && q.data.length === 0 && <Empty>No courses. Import a catalog first.</Empty>}
      {q.data && q.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Code</th>
                <th>Title</th>
                <th>Credits</th>
                <th>Type</th>
                <th>Offer Terms</th>
                <th>Cadence</th>
              </tr>
            </thead>
            <tbody>
              {q.data.map((c) => (
                <tr key={c.code}>
                  <td className="mono">{c.code}</td>
                  <td style={{ whiteSpace: "normal", maxWidth: 320 }}>{c.title}</td>
                  <td>{c.credits}</td>
                  <td>{c.structure_type}</td>
                  <td>{c.offer_terms.length > 0 ? c.offer_terms.join(", ") : <span className="subtle">any</span>}</td>
                  <td>{c.cadence}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function FacultyTab() {
  const q = useAsync(useCallback(() => api.listFaculty(), []), []);
  if (q.loading) return <Spinner />;
  if (q.error) return <ErrorBox message={q.error} />;
  if (!q.data || q.data.length === 0) return <Empty>No faculty. Import faculty first.</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Name</th>
            <th>Email</th>
            <th>Max Credits</th>
            <th>Approved Courses</th>
          </tr>
        </thead>
        <tbody>
          {q.data.map((f) => (
            <tr key={f.id}>
              <td className="mono">{f.id}</td>
              <td>{f.name}</td>
              <td className="subtle">{f.email}</td>
              <td>{f.max_credits}</td>
              <td style={{ whiteSpace: "normal", maxWidth: 320 }}>
                {f.approved_courses.length > 0 ? (
                  f.approved_courses.join(", ")
                ) : (
                  <span className="subtle">any</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RoomsTab() {
  const q = useAsync(useCallback(() => api.listRooms(), []), []);
  if (q.loading) return <Spinner />;
  if (q.error) return <ErrorBox message={q.error} />;
  if (!q.data || q.data.length === 0) return <Empty>No rooms. Import rooms first.</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Building</th>
            <th>Number</th>
            <th>Capacity</th>
            <th>Type</th>
          </tr>
        </thead>
        <tbody>
          {q.data.map((r) => (
            <tr key={r.id}>
              <td className="mono">{r.id}</td>
              <td>{r.building}</td>
              <td>{r.number}</td>
              <td>{r.capacity}</td>
              <td>{r.room_type}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
