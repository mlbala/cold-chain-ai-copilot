import { CloudLightning, PiggyBank, Thermometer, Truck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import type { FleetSummary } from "../api/client";
import type { PollState } from "../hooks/usePolling";
import { Meter, Skeleton, timeAgo, toneText, usd } from "./ui";
import type { Tone } from "./ui";

function KpiCard(props: {
  label: string;
  icon: LucideIcon;
  tone: Tone;
  value: ReactNode;
  detail: ReactNode;
  share?: { value: number; max: number; label: string };
  loading: boolean;
}) {
  const Icon = props.icon;
  return (
    <div className="card group relative overflow-hidden p-4">
      <div className="flex items-start justify-between gap-3">
        <span className="label-caps">{props.label}</span>
        <span className={`rounded-lg bg-surface-2 p-1.5 ${toneText[props.tone]}`}>
          <Icon className="h-4 w-4" aria-hidden />
        </span>
      </div>
      {props.loading ? (
        <div className="mt-2 space-y-2">
          <Skeleton className="h-8 w-16" />
          <Skeleton className="h-3 w-32" />
        </div>
      ) : (
        <>
          <div
            className={`mt-1 text-[28px] leading-tight font-semibold tabular-nums ${
              props.tone === "danger" || props.tone === "warn" ? toneText[props.tone] : "text-fg"
            }`}
          >
            {props.value}
          </div>
          <div className="mt-0.5 truncate text-xs text-fg-subtle">{props.detail}</div>
          {props.share ? (
            <div className="mt-3 flex items-center gap-2">
              <Meter value={props.share.value} max={props.share.max} tone={props.tone} label={props.share.label} />
              <span className="text-[11px] text-fg-subtle tabular-nums">
                {props.share.max ? Math.round((props.share.value / props.share.max) * 100) : 0}%
              </span>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

export default function TelemetryRibbon({ state }: { state: PollState<FleetSummary> }) {
  const summary = state.data;
  const loading = summary === null && state.error === null;
  const savings = summary?.cost_savings;
  const calls = savings ? savings.flash_calls + savings.pro_calls : 0;

  return (
    <section aria-label="Live fleet KPIs" className="space-y-2">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KpiCard
          label="Active fleet"
          icon={Truck}
          tone="accent"
          loading={loading}
          value={summary?.active_fleet ?? "—"}
          detail={summary ? `${summary.low_battery} low battery · ${summary.doors_open} door open` : "—"}
        />
        <KpiCard
          label="Temp excursions"
          icon={Thermometer}
          tone={summary && summary.temp_excursions > 0 ? "danger" : "ok"}
          loading={loading}
          value={summary?.temp_excursions ?? "—"}
          detail="outside target band"
          share={summary ? { value: summary.temp_excursions, max: summary.active_fleet, label: "Share of fleet in excursion" } : undefined}
        />
        <KpiCard
          label="Weather alerts"
          icon={CloudLightning}
          tone={summary && summary.weather_alerts > 0 ? "warn" : "ok"}
          loading={loading}
          value={summary?.weather_alerts ?? "—"}
          detail="trucks on weather hold"
          share={summary ? { value: summary.weather_alerts, max: summary.active_fleet, label: "Share of fleet on weather hold" } : undefined}
        />
        <KpiCard
          label="Cost savings"
          icon={PiggyBank}
          tone={calls ? "ok" : "neutral"}
          loading={loading}
          value={savings && calls ? `${savings.saved_pct.toFixed(0)}%` : "—"}
          detail={
            savings && calls
              ? `${usd(savings.saved_usd)} vs all-Pro · ${savings.flash_share_pct.toFixed(0)}% Flash`
              : "hybrid routing vs all-Pro"
          }
          share={savings && calls ? { value: savings.flash_share_pct, max: 100, label: "Flash share of model calls" } : undefined}
        />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 px-1 text-[11px] text-fg-subtle">
        <span>
          {state.error ? (
            <span className="text-danger">{state.error}</span>
          ) : summary ? (
            <>Last truck ping {timeAgo(summary.last_ping_utc)} · refreshed {timeAgo(summary.generated_at_utc)}</>
          ) : (
            "Connecting to telemetry…"
          )}
        </span>
        {savings && calls ? (
          <span>
            Prompt cache hits {savings.cache_hit_pct.toFixed(0)}%
            {savings.cache_priced ? ` (${usd(savings.cache_saved_usd)} saved)` : ""}
            {savings.input_tokens_only ? " · savings on input-token prices" : ""}
          </span>
        ) : null}
      </div>
    </section>
  );
}
