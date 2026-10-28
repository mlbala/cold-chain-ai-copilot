import { Gauge, MessageSquare, ShieldCheck, Snowflake } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api } from "./api/client";
import AuditDashboard from "./components/AuditDashboard";
import DispatchChat from "./components/DispatchChat";
import FleetBoard from "./components/FleetBoard";
import RiskHealthDashboard from "./components/RiskHealthDashboard";
import TelemetryRibbon from "./components/TelemetryRibbon";
import ThemeToggle from "./components/ThemeToggle";
import { StatusDot } from "./components/ui";
import { usePolling } from "./hooks/usePolling";

type Tab = "dispatch" | "risk" | "audit";

const TABS: { id: Tab; label: string; short: string; icon: LucideIcon }[] = [
  { id: "dispatch", label: "Dispatch Console", short: "Dispatch", icon: MessageSquare },
  { id: "risk", label: "Risk & Health", short: "Risk", icon: Gauge },
  { id: "audit", label: "Security Audit", short: "Audit", icon: ShieldCheck },
];

function tabFromHash(): Tab {
  const hash = window.location.hash.replace("#", "");
  return TABS.some((t) => t.id === hash) ? (hash as Tab) : "dispatch";
}

function HealthPill() {
  const health = usePolling(api.health, 60_000);
  const ok = health.data?.status === "ok";
  const state = health.error ? "down" : health.data ? (ok ? "ok" : "degraded") : "unknown";
  const label = health.error ? "API offline" : health.data ? (ok ? "Operational" : "Degraded") : "Checking";
  return (
    <span className="hidden items-center gap-2 rounded-full border border-line bg-surface px-2.5 py-1 text-xs text-fg-muted sm:inline-flex">
      <StatusDot state={state} pulse={state !== "ok"} />
      {label}
      {health.data ? <span className="text-fg-subtle">v{health.data.version}</span> : null}
    </span>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const [refreshKey, setRefreshKey] = useState(0);
  const [prefill, setPrefill] = useState<{ id: number; text: string } | null>(null);
  const fleet = usePolling(api.fleetSummary, 30_000, refreshKey);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const current = TABS.find((t) => t.id === tab);
    document.title = current ? `${current.label} · Cold Chain Control Tower` : "Cold Chain Control Tower";
  }, [tab]);

  const go = useCallback((next: Tab) => {
    setTab(next);
    if (window.location.hash !== `#${next}`) window.history.replaceState(null, "", `#${next}`);
  }, []);

  const ask = useCallback(
    (text: string) => {
      setPrefill({ id: Date.now(), text });
      go("dispatch");
      window.scrollTo({ top: 0, behavior: "smooth" });
    },
    [go],
  );

  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-30 border-b border-line bg-canvas/80 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-[1440px] items-center gap-2 px-4 sm:gap-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-pro text-white shadow-pop">
              <Snowflake className="h-5 w-5" aria-hidden />
            </div>
            <div className="hidden min-w-0 sm:block">
              <div className="truncate text-[14px] leading-tight font-semibold text-fg">Cold Chain Control Tower</div>
              <div className="truncate text-[11px] text-fg-subtle">Telemetry · weather · SOP copilot</div>
            </div>
          </div>

          <nav aria-label="Workspace" className="mx-auto flex rounded-xl border border-line bg-surface-2 p-1">
            {TABS.map(({ id, label, short, icon: Icon }) => {
              const active = tab === id;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => go(id)}
                  aria-current={active ? "page" : undefined}
                  aria-label={label}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[13px] whitespace-nowrap transition-colors sm:px-3 ${
                    active
                      ? "bg-accent font-semibold text-accent-fg shadow-card"
                      : "font-medium text-fg-muted hover:bg-surface-3 hover:text-fg"
                  }`}
                >
                  <Icon className="h-4 w-4" aria-hidden />
                  <span className="hidden md:inline">{label}</span>
                  <span className="hidden sm:inline md:hidden">{short}</span>
                </button>
              );
            })}
          </nav>

          <div className="flex shrink-0 items-center gap-2">
            <HealthPill />
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1440px] space-y-5 px-4 py-5 sm:px-6">
        <TelemetryRibbon state={fleet} />

        <div hidden={tab !== "dispatch"} className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
          <DispatchChat onTurnComplete={() => setRefreshKey((value) => value + 1)} prefill={prefill} />
          <FleetBoard state={fleet} onAsk={ask} />
        </div>
        {tab === "risk" ? <RiskHealthDashboard onAsk={ask} /> : null}
        {tab === "audit" ? <AuditDashboard /> : null}
      </main>
    </div>
  );
}
