import { useCallback, useState } from "react";
import { api, ApiError } from "../api";
import type { ProjectionInfo } from "../api";
import { useAsync } from "../useAsync";
import { Spinner, ErrorBox, Empty } from "../components";

export function ProjectionsView() {
  const projections = useAsync(useCallback(() => api.listProjections(), []), []);

  const [major, setMajor] = useState("");
  const [count, setCount] = useState(0);
  const [catalogYear, setCatalogYear] = useState(new Date().getFullYear());
  const [semester, setSemester] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function save() {
    if (!major.trim() || count <= 0) {
      setMsg({ ok: false, text: "Enter a major and a positive estimated count." });
      return;
    }
    setSaving(true);
    setMsg(null);
    try {
      const p = await api.createProjection({
        major: major.trim().toUpperCase(),
        estimated_count: count,
        catalog_year: catalogYear,
        semester: semester.trim(),
      });
      setMsg({ ok: true, text: `Saved projection: ${p.estimated_count} ${p.major} freshmen.` });
      setMajor("");
      setCount(0);
      projections.reload();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setSaving(false);
    }
  }

  async function remove(p: ProjectionInfo) {
    if (!confirm(`Delete the projection for ${p.estimated_count} ${p.major} freshmen?`)) return;
    try {
      await api.deleteProjection(p.id);
      projections.reload();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof ApiError ? err.message : String(err) });
    }
  }

  return (
    <div>
      <h1>Freshman Projections</h1>
      <p className="subtle">
        Estimate the incoming freshman class per major. When you generate a schedule with{" "}
        <strong>“Include projected freshman class”</strong> checked, the solver adds synthetic
        demand for these students — so intro courses are sized for freshmen who aren’t in Banner
        yet. The major must already have degree requirements loaded for the chosen catalog year.
      </p>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap", gap: 12 }}>
          <div>
            <label htmlFor="proj-major">Major</label>
            <input
              id="proj-major"
              value={major}
              placeholder="BIOL"
              onChange={(e) => setMajor(e.target.value)}
              disabled={saving}
              style={{ width: 120, textTransform: "uppercase" }}
            />
          </div>
          <div>
            <label htmlFor="proj-count">Estimated count</label>
            <input
              id="proj-count"
              type="number"
              min={1}
              value={count || ""}
              onChange={(e) => setCount(Number(e.target.value))}
              disabled={saving}
              style={{ width: 120 }}
            />
          </div>
          <div>
            <label htmlFor="proj-year">Catalog year</label>
            <input
              id="proj-year"
              type="number"
              min={2000}
              max={2100}
              value={catalogYear}
              onChange={(e) => setCatalogYear(Number(e.target.value))}
              disabled={saving}
              style={{ width: 110 }}
            />
          </div>
          <div>
            <label htmlFor="proj-sem">Semester (optional)</label>
            <input
              id="proj-sem"
              value={semester}
              placeholder="2026FA"
              onChange={(e) => setSemester(e.target.value)}
              disabled={saving}
              style={{ width: 110 }}
            />
          </div>
          <div className="spacer" />
          <button onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Add / Update"}
          </button>
        </div>
        {msg && (
          <div
            className={msg.ok ? "banner banner-ok" : "banner banner-err"}
            style={{ marginTop: 10 }}
          >
            {msg.text}
          </div>
        )}
      </div>

      {projections.loading && <Spinner />}
      {projections.error && <ErrorBox message={projections.error} />}
      {projections.data && projections.data.length === 0 && (
        <Empty>No projections yet. Add one above to size intro courses for incoming freshmen.</Empty>
      )}
      {projections.data && projections.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Major</th>
                <th>Estimated freshmen</th>
                <th>Catalog year</th>
                <th>Semester</th>
                <th>Active</th>
                <th style={{ width: 80 }} />
              </tr>
            </thead>
            <tbody>
              {projections.data
                .slice()
                .sort((a, b) => a.major.localeCompare(b.major))
                .map((p) => (
                  <tr key={p.id}>
                    <td>{p.major}</td>
                    <td>{p.estimated_count}</td>
                    <td>{p.catalog_year}</td>
                    <td>{p.semester || "—"}</td>
                    <td>
                      {p.is_active ? (
                        <span className="pill pill-approved">Active</span>
                      ) : (
                        <span className="pill pill-archived">Inactive</span>
                      )}
                    </td>
                    <td>
                      <button className="ghost" onClick={() => remove(p)}>
                        Delete
                      </button>
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
