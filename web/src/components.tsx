import type { ReactNode } from "react";

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="state state-loading">{label}</div>;
}

export function ErrorBox({ message }: { message: string }) {
  return <div className="state state-error">Error: {message}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state state-empty">{children}</div>;
}

export function StatusPill({ status }: { status: string }) {
  const cls = `pill pill-${status.toLowerCase()}`;
  return <span className={cls}>{status}</span>;
}
