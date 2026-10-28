import { CloudLightning, PiggyBank, Thermometer, Truck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api/client";
import type { FleetSummary } from "../api/client";

const POLL_MS = 30_000;

type Tone = "neutral" | "danger" | "warn" | "ok";

const toneStyles: Record<Tone, { ring: string; icon: string; value: string }> = {
  neutral: { ring: "border-ink-700", icon: "text-signal bg-signal/10", value: "text-ink-100" },
  danger: { ring: "border-danger/50", icon: "text-danger bg-danger/10", value: "text-danger" },
  warn: { ring: "border-warn/50", icon: "text-warn bg-warn/10", value: "text-warn" },
  ok: { ring: "border-ok/40", icon: "text-ok bg-ok/10", value: "text-ok" },
};

function KpiTile(props: {
  label: string;
  value: string;
  detail: string;
  icon: LucideIcon;
  tone: Tone;
}) {
  const Icon = props.icon;
  const tone = toneStyles[props.tone];
  return (
    <div className={`panel flex items-center gap-3 border p-3 sm:p-4 ${tone.ring}`}>
      <div className={`rounded-lg p-2 ${tone.icon}`}>
        <Icon className="h-5 w-5" aria-hidden />
      </div>
      <div className="min-w-0">
        <div className="text-[11px] font-medium uppercase tracking-wider text-ink-400">
          {props.label}
        </div>
        <div className={`text-2xl font-semibold tabular-nums ${tone.value}`}>{props.value}</div>
        <div className="truncate text-xs text-ink-400">{props.detail}</div>
      </div>
    </div>
  );
}

function minutesAgo(iso: string | null): string {
  if (!iso) return "no pings yet";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  return minutes === 0 ? "last ping just now" : `last ping ${minutes} min ago`;
}

function usd(value: number): string {
  if (value === 0) return "$0";
  return value < 0.01 ? `$${value.toFixed(5)}` : `$${value.toFixed(2)}`;
}

export default function TelemetryRibbon({ refreshKey }: { refreshKey: number }) {
  const [summary, setSummary] = useState<FleetSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSummary(await api.fleetSummary());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Telemetry unavailable");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, refreshKey]);

  const savings = summary?.cost_savings;
  const hasCalls = savings ? savings.flash_calls + savings.pro_calls > 0 : false;

  return (
    <section aria-label="Live fleet KPIs" className="space-y-2">
      <div className="grid grid-cols-2 gap-2 sm:gap-3 lg:grid-cols-4">
        <KpiTile
          label="Active Fleet"
          icon={Truck}
          tone="neutral"
          value={summary ? String(summary.active_fleet) : "—"}
          detail={
            summary
              ? `${summary.low_battery} low battery · ${summary.doors_open} door open`
              : "loading telemetry"
          }
        />
        <KpiTile
          label="Temp Excursions"
          icon={Thermometer}
          tone={summary && summary.temp_excursions > 0 ? "danger" : "ok"}
          value={summary ? String(summary.temp_excursions) : "—"}
          detail="outside target band"
        />
        <KpiTile
          label="Weather Alerts"
          icon={CloudLightning}
          tone={summary && summary.weather_alerts > 0 ? "warn" : "ok"}
          value={summary ? String(summary.weather_alerts) : "—"}
          detail="trucks on weather hold"
        />
        <KpiTile
          label="Cost Savings"
          icon={PiggyBank}
          tone={hasCalls ? "ok" : "neutral"}
          value={savings && hasCalls ? `${savings.saved_pct.toFixed(1)}%` : "—"}
          detail={
            savings && hasCalls
              ? `${usd(savings.saved_usd)} vs all-Pro · ${savings.flash_share_pct.toFixed(0)}% Flash`
              : "hybrid routing vs all-Pro"
          }
        />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 px-1 text-xs text-ink-400">
        <span>
          {error ? (
            <span className="text-danger">{error}</span>
          ) : summary ? (
            <>
              {minutesAgo(summary.last_ping_utc)} · refreshed{" "}
              {new Date(summary.generated_at_utc).toLocaleTimeString()}
            </>
          ) : (
            "connecting…"
          )}
        </span>
        {savings && hasCalls ? (
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
