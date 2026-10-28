import { MessageSquare, ShieldCheck, Snowflake } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "./api/client";
import type { HealthResponse } from "./api/client";
import AuditDashboard from "./components/AuditDashboard";
import DispatchChat from "./components/DispatchChat";
import TelemetryRibbon from "./components/TelemetryRibbon";

type Tab = "dispatch" | "audit";

const TABS: { id: Tab; label: string; icon: LucideIcon }[] = [
  { id: "dispatch", label: "Autonomous Dispatch Console", icon: MessageSquare },
  { id: "audit", label: "Security Audit", icon: ShieldCheck },
];

function HealthPill() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function check() {
      try {
        const result = await api.health();
        if (!cancelled) {
          setHealth(result);
          setReachable(true);
        }
      } catch {
        if (!cancelled) setReachable(false);
      }
    }
    void check();
    const timer = window.setInterval(() => void check(), 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const ok = reachable && health?.status === "ok";
  const label = !reachable ? "API offline" : health ? (ok ? "All systems normal" : "Degraded") : "Checking…";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
        ok ? "border-ok/40 text-ok" : "border-warn/40 text-warn"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-ok" : "bg-warn"}`} aria-hidden />
      {label}
      {health ? <span className="text-ink-400">· v{health.version}</span> : null}
    </span>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("dispatch");
  const [ribbonKey, setRibbonKey] = useState(0);

  return (
    <div className="mx-auto flex min-h-dvh max-w-7xl flex-col gap-4 px-4 py-4 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="rounded-lg border border-ink-700 bg-ink-900 p-2">
            <Snowflake className="h-5 w-5 text-signal" aria-hidden />
          </div>
          <div>
            <h1 className="text-base font-semibold leading-tight">Cold Chain Control Tower</h1>
            <p className="text-xs text-ink-400">Fleet telemetry · live weather · SOP copilot</p>
          </div>
        </div>
        <HealthPill />
      </header>

      <TelemetryRibbon refreshKey={ribbonKey} />

      <nav aria-label="Workspace" className="flex gap-1 overflow-x-auto rounded-xl border border-ink-700 bg-ink-900 p-1">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            aria-current={tab === id ? "page" : undefined}
            className={`inline-flex items-center gap-2 whitespace-nowrap rounded-lg px-3 py-2 text-sm transition-colors ${
              tab === id
                ? "bg-ink-700 text-ink-100"
                : "text-ink-400 hover:bg-ink-800 hover:text-ink-100"
            }`}
          >
            <Icon className="h-4 w-4" aria-hidden />
            {label}
          </button>
        ))}
      </nav>

      <main className="flex-1">
        <div hidden={tab !== "dispatch"}>
          <DispatchChat onTurnComplete={() => setRibbonKey((value) => value + 1)} />
        </div>
        {tab === "audit" ? <AuditDashboard /> : null}
      </main>
    </div>
  );
}
