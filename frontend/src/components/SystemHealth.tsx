import { Activity, Database, Gauge, Network, RefreshCw, ShieldCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useMemo } from "react";

import type { GuardrailStatus, SystemStatus } from "../api/client";
import type { PollState } from "../hooks/usePolling";
import { Button, Card, CardHeader, Meter, Pill, Skeleton, StatusDot, stateTone, timeAgo, toneText, usd } from "./ui";

export function SystemHealth({ state }: { state: PollState<SystemStatus> }) {
  const status = state.data;
  if (!status) {
    return (
      <Card>
        <CardHeader icon={Gauge} title="System health" subtitle="Probing dependencies…" />
        <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-20 rounded-xl" />
          ))}
        </div>
      </Card>
    );
  }
  const headline = { ok: "All systems normal", degraded: "Degraded", down: "Outage", unknown: "Unknown" }[status.status];
  const budget = status.llm_daily_budget_usd;
  return (
    <Card>
      <CardHeader
        icon={Gauge}
        title="System health"
        subtitle={`v${status.version} · ${status.environment} · up ${Math.floor(status.uptime_s / 3600)}h ${Math.floor((status.uptime_s % 3600) / 60)}m · checked ${timeAgo(status.generated_at_utc)}`}
        actions={
          <Button size="sm" icon={RefreshCw} onClick={state.refresh} disabled={state.loading}>
            Refresh
          </Button>
        }
      />
      <div className="grid gap-3 border-b border-line p-4 md:grid-cols-[auto_1fr_1fr]">
        <div className="flex items-center gap-3">
          <StatusDot state={status.status} pulse={status.status !== "ok"} />
          <span className={`text-sm font-semibold ${toneText[stateTone[status.status]]}`}>{headline}</span>
        </div>
        <div>
          <div className="flex justify-between text-[11px] text-fg-subtle">
            <span>Model spend today</span>
            <span className="tabular-nums">
              {usd(status.llm_spend_today_usd)} {budget ? `of ${usd(budget)}` : "· no cap"}
            </span>
          </div>
          <Meter className="mt-1.5" value={status.llm_spend_today_usd} max={budget || 1} tone={budget && status.llm_spend_today_usd / budget > 0.8 ? "warn" : "ok"} label="Daily model budget used" />
        </div>
        <div>
          <div className="flex justify-between text-[11px] text-fg-subtle">
            <span>Conversation memory</span>
            <span className="tabular-nums">
              {status.active_sessions} / {status.max_sessions}
            </span>
          </div>
          <Meter className="mt-1.5" value={status.active_sessions} max={status.max_sessions} tone="accent" label="Sessions in memory" />
        </div>
      </div>
      <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
        {status.components.map((component) => (
          <div key={component.key} className="rounded-xl border border-line bg-surface-2/50 p-3">
            <div className="flex items-center gap-2">
              <StatusDot state={component.status} />
              <span className="text-xs font-semibold text-fg">{component.name}</span>
              {component.latency_ms !== null ? (
                <span className="ml-auto text-[11px] text-fg-subtle tabular-nums">{component.latency_ms} ms</span>
              ) : null}
            </div>
            <p className="mt-1.5 text-[11.5px] leading-snug text-fg-muted">{component.detail}</p>
          </div>
        ))}
      </div>
    </Card>
  );
}

const LAYERS: { id: GuardrailStatus["layer"]; label: string; icon: LucideIcon }[] = [
  { id: "database", label: "Database", icon: Database },
  { id: "sql", label: "SQL", icon: ShieldCheck },
  { id: "agent", label: "Agent", icon: Activity },
  { id: "api", label: "API", icon: Network },
  { id: "audit", label: "Audit", icon: ShieldCheck },
];

export function Guardrails({ status }: { status: SystemStatus | null }) {
  const total = useMemo(
    () => (status ? status.guardrails.reduce((sum, g) => sum + (g.triggered ?? 0), 0) : 0),
    [status],
  );
  return (
    <Card>
      <CardHeader
        icon={ShieldCheck}
        title="Guardrails"
        subtitle={
          status
            ? `${status.guardrails.length} active controls · ${total} activations since start`
            : "Loading guardrail inventory…"
        }
      />
      {!status ? (
        <div className="grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-24 rounded-xl" />
          ))}
        </div>
      ) : (
        <div className="space-y-5 p-4">
          {LAYERS.map((layer) => {
            const items = status.guardrails.filter((g) => g.layer === layer.id);
            if (!items.length) return null;
            const Icon = layer.icon;
            return (
              <section key={layer.id}>
                <div className="mb-2 flex items-center gap-2">
                  <Icon className="h-3.5 w-3.5 text-accent" aria-hidden />
                  <span className="label-caps">{layer.label}</span>
                  <span className="h-px flex-1 bg-line" aria-hidden />
                </div>
                <div className="grid gap-2.5 md:grid-cols-2 xl:grid-cols-3">
                  {items.map((g) => (
                    <div key={g.key} className="rounded-xl border border-line bg-surface p-3">
                      <div className="flex items-start justify-between gap-2">
                        <span className="text-[13px] font-semibold text-fg">{g.name}</span>
                        {g.triggered === null ? (
                          <Pill tone="ok">enforced</Pill>
                        ) : (
                          <Pill tone={g.triggered ? "warn" : "ok"} title="Activations since the API started">
                            {g.triggered ? `${g.triggered} blocked` : "0 triggered"}
                          </Pill>
                        )}
                      </div>
                      <div className="mt-1.5 font-mono text-[11px] text-accent">{g.setting}</div>
                      <p className="mt-1 text-[11.5px] leading-snug text-fg-subtle">{g.description}</p>
                    </div>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </Card>
  );
}
