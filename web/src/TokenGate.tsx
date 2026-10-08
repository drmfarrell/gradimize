import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getToken, setToken } from "./api";

type GateState = "checking" | "open" | "locked";

// Blocks the app behind the shared access token when the server has
// GRADIMIZE_TOKEN set. The /api root reports auth_required without auth;
// if auth is on, an authenticated probe decides whether the stored token
// still works, and a minimal prompt collects it otherwise.
export function TokenGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<GateState>("checking");
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);

  const probe = useCallback(async () => {
    setState("checking");
    setError(null);
    try {
      const root = await api.apiRoot();
      if (!root.auth_required) {
        setState("open");
        return;
      }
      await api.optimizationStatus(); // any authenticated endpoint works
      setState("open");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        if (getToken()) setError("Stored access token was rejected.");
        setState("locked");
      } else {
        // Server unreachable etc. — let the app render and surface its
        // own errors rather than dead-ending on the gate.
        setState("open");
      }
    }
  }, []);

  useEffect(() => {
    const run = () => probe();
    void run();
  }, [probe]);

  if (state === "open") return <>{children}</>;
  if (state === "checking") {
    return <div className="state state-empty">Connecting…</div>;
  }

  return (
    <div style={{ maxWidth: 420, margin: "18vh auto", padding: 24 }} className="card">
      <h2 style={{ marginTop: 0 }}>Gradimize</h2>
      <p className="subtle">
        This server requires an access token. Enter the token provided by the
        Gradimize administrator.
      </p>
      {error && <div className="banner banner-err">{error}</div>}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setToken(input.trim());
          setInput("");
          probe();
        }}
      >
        <div className="row">
          <input
            type="password"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Access token"
            autoFocus
            style={{ flex: 1 }}
          />
          <button type="submit" disabled={!input.trim()}>
            Unlock
          </button>
        </div>
      </form>
    </div>
  );
}
