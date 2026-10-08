import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import type { OptimizationStatus } from "./api";

// Single shared poller for the server's optimization lock, so the whole UI can
// react to a solve in progress (its own or another user's) without each button
// polling independently.
const Ctx = createContext<OptimizationStatus>({ running: false });

const KIND_LABEL: Record<string, string> = {
  generate: "Generating a schedule",
  "re-solve": "Re-optimizing a schedule",
  finals: "Solving a finals schedule",
};

export function optimizationKindLabel(kind?: string): string {
  return (kind && KIND_LABEL[kind]) || "An optimization";
}

export function OptimizationStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<OptimizationStatus>({ running: false });

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await api.optimizationStatus();
        if (alive) setStatus(s);
      } catch {
        // transient error — keep the last known status
      }
    };
    tick();
    const id = setInterval(tick, 2500);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return <Ctx.Provider value={status}>{children}</Ctx.Provider>;
}

export function useOptimizationStatus(): OptimizationStatus {
  return useContext(Ctx);
}
