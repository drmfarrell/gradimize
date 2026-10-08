import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { ImportFileResponse, ImportKindInfo } from "../api";
import { useAsync } from "../useAsync";
import { Spinner, ErrorBox } from "../components";

type RowState =
  | { status: "idle" }
  | { status: "uploading"; filename: string }
  | { status: "done"; result: ImportFileResponse }
  | { status: "error"; message: string };

export function ImportView() {
  const kinds = useAsync(useCallback(() => api.importKinds(), []), []);
  const [rows, setRows] = useState<Record<string, RowState>>({});

  function setRow(key: string, state: RowState) {
    setRows((prev) => ({ ...prev, [key]: state }));
  }

  async function upload(kind: ImportKindInfo, file: File) {
    setRow(kind.key, { status: "uploading", filename: file.name });
    try {
      const result = await api.importFile(kind.key, file);
      setRow(kind.key, { status: "done", result });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : String(err);
      setRow(kind.key, { status: "error", message });
    }
  }

  return (
    <div>
      <h1>Import Data</h1>
      <p className="subtle">
        Upload the input files the solver needs. Each upload <strong>replaces</strong> the
        existing rows of that kind. Upload in the order shown — later files reference earlier
        ones (e.g. prerequisites need the catalog).
      </p>

      {kinds.loading && <Spinner />}
      {kinds.error && <ErrorBox message={kinds.error} />}
      {kinds.data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 36 }}>#</th>
                <th>File</th>
                <th>Description</th>
                <th style={{ width: 160 }}>Upload</th>
                <th style={{ width: 220 }}>Result</th>
              </tr>
            </thead>
            <tbody>
              {kinds.data.map((kind, i) => (
                <ImportRow
                  key={kind.key}
                  index={i + 1}
                  kind={kind}
                  state={rows[kind.key] ?? { status: "idle" }}
                  onUpload={upload}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ImportRow({
  index,
  kind,
  state,
  onUpload,
}: {
  index: number;
  kind: ImportKindInfo;
  state: RowState;
  onUpload: (kind: ImportKindInfo, file: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <tr>
      <td className="subtle">{index}</td>
      <td>
        <div>
          <strong>{kind.label}</strong>
        </div>
        <div className="subtle mono" style={{ fontSize: 11 }}>
          {kind.filename}
        </div>
      </td>
      <td className="subtle" style={{ whiteSpace: "normal", maxWidth: 380 }}>
        {kind.description}
      </td>
      <td>
        <input
          ref={inputRef}
          type="file"
          accept={kind.is_yaml ? ".yaml,.yml" : ".csv"}
          style={{ display: "none" }}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onUpload(kind, file);
            e.target.value = ""; // allow re-selecting the same file
          }}
        />
        <button
          className="ghost"
          onClick={() => inputRef.current?.click()}
          disabled={state.status === "uploading"}
        >
          {state.status === "uploading" ? "Uploading…" : "Choose file"}
        </button>
      </td>
      <td>
        <ResultCell state={state} />
      </td>
    </tr>
  );
}

function ResultCell({ state }: { state: RowState }) {
  if (state.status === "idle") return <span className="subtle">—</span>;
  if (state.status === "uploading")
    return <span className="subtle mono">{state.filename}…</span>;
  if (state.status === "error")
    return <span style={{ color: "var(--red)" }}>{state.message}</span>;

  // done
  const { result } = state;
  const hasErrors = result.errors.length > 0;
  return (
    <div>
      <span style={{ color: hasErrors ? "var(--yellow)" : "var(--green)" }}>
        {result.rows_loaded} rows loaded
        {result.cleared ? " (replaced)" : ""}
      </span>
      {hasErrors && (
        <details style={{ marginTop: 4 }}>
          <summary className="subtle" style={{ cursor: "pointer" }}>
            {result.errors.length} warning{result.errors.length > 1 ? "s" : ""}
          </summary>
          <ul style={{ margin: "4px 0", paddingLeft: 18 }}>
            {result.errors.slice(0, 20).map((e, i) => (
              <li key={i} className="subtle" style={{ fontSize: 12 }}>
                {e}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
