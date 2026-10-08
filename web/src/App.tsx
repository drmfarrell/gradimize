import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  OptimizationStatusProvider,
  optimizationKindLabel,
  useOptimizationStatus,
} from "./optimizationStatus";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/schedules", label: "Schedules", end: false },
  { to: "/finals", label: "Finals", end: false },
  { to: "/conflicts", label: "Conflicts", end: false },
  { to: "/projections", label: "Projections", end: false },
  { to: "/data", label: "Data", end: false },
  { to: "/import", label: "Import", end: false },
];

function BetaNotice() {
  const [show, setShow] = useState(true);
  if (!show) return null;
  return (
    <div
      style={{
        background: "rgba(210,153,34,0.12)",
        borderBottom: "1px solid rgba(210,153,34,0.35)",
        color: "var(--yellow, #d29922)",
        padding: "6px 16px",
        fontSize: 13,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
        textAlign: "center",
      }}
    >
      <span>
        ⚠ <strong>Beta</strong> — prototype running on <strong>synthetic data</strong>.
        Backend is 2013-era hardware (AMD Opteron · 128&nbsp;GB DDR3), so optimizations
        can be slow.
      </span>
      <button
        onClick={() => setShow(false)}
        aria-label="Dismiss"
        style={{
          background: "transparent",
          border: "none",
          color: "inherit",
          cursor: "pointer",
          fontSize: 15,
          lineHeight: 1,
          padding: "0 4px",
          width: "auto",
        }}
      >
        ×
      </button>
    </div>
  );
}

function OptimizationBanner() {
  const status = useOptimizationStatus();
  if (!status.running) return null;
  return (
    <div
      style={{
        background: "rgba(210,153,34,0.15)",
        borderBottom: "1px solid rgba(210,153,34,0.4)",
        color: "var(--yellow, #d29922)",
        padding: "8px 16px",
        fontSize: 14,
        textAlign: "center",
      }}
    >
      ⏳ {optimizationKindLabel(status.kind)}
      {status.label ? ` (“${status.label}”)` : ""} is in progress. This is a
      single-user system, so optimize controls are disabled until it finishes.
    </div>
  );
}

export function App() {
  return (
    <OptimizationStatusProvider>
      <div className="app">
        <header className="topbar">
          <div className="brand">Gradimize</div>
          <nav className="nav">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </header>
        <BetaNotice />
        <OptimizationBanner />
        <main className="content">
          <Outlet />
        </main>
      </div>
    </OptimizationStatusProvider>
  );
}
