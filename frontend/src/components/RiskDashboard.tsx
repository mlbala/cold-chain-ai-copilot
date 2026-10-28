import {
  Activity,
  ArrowRight,
  CloudLightning,
  MessageSquarePlus,
  Network,
  Pill as PillIcon,
  ShieldCheck,
  Siren,
  TriangleAlert,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

import { api } from "../api/client";
import type { RiskBucket, RiskLevel, RiskOverview, RiskShipment } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import {
  Card,
  CardHeader,
  EmptyState,
  LevelBadge,
  Meter,
  Pill,
  ScoreRing,
  Skeleton,
  levelTone,
  toneText,
} from "./ui";
import type { Tone } from "./ui";

const LEVELS: RiskLevel[] = ["critical", "high", "elevated", "normal"];
const LEVEL_FILL: Record<RiskLevel, string> = {
  critical: "bg-danger",
  high: "bg-warn",
  elevated: "bg-accent",
  normal: "bg-surface-3",
};

function StatTile(props: { label: string; value: number | string; tone: Tone; icon: LucideIcon; hint: string }) {
  const Icon = props.icon;
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between">
        <span className="label-caps">{props.label}</span>
        <Icon className={`h-4 w-4 ${toneText[props.tone]}`} aria-hidden />
      </div>
      <div className={`mt-1 text-[26px] leading-tight font-semibold tabular-nums ${props.tone === "neutral" ? "text-fg" : toneText[props.tone]}`}>
        {props.value}
      </div>
      <div className="text-xs text-fg-subtle">{props.hint}</div>
    </div>
  );
}

function RiskSummary({ risk }: { risk: RiskOverview }) {
  const total = risk.shipments.length || 1;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatTile label="Critical" value={risk.totals.critical} tone="danger" icon={Siren} hint="act within minutes" />
        <StatTile label="High" value={risk.totals.high} tone="warn" icon={TriangleAlert} hint="needs a dispatcher" />
        <StatTile label="Elevated" value={risk.totals.elevated} tone="accent" icon={Activity} hint="watch closely" />
        <StatTile label="Nominal" value={risk.totals.normal} tone="ok" icon={ShieldCheck} hint="no risk factors" />
        <StatTile label="Pharma at risk" value={risk.pharma_at_risk} tone={risk.pharma_at_risk ? "danger" : "ok"} icon={PillIcon} hint="vaccines, biologics, plasma, insulin" />
        <StatTile label="Weather delays" value={risk.delayed} tone={risk.delayed ? "warn" : "ok"} icon={CloudLightning} hint="on weather hold" />
      </div>
      <div className="card flex items-center gap-4 px-4 py-3">
        <span className="label-caps shrink-0">Fleet risk mix</span>
        <div className="flex h-2.5 flex-1 overflow-hidden rounded-full bg-surface-3" aria-label="Risk distribution">
          {LEVELS.map((level) =>
            risk.totals[level] ? (
              <div
                key={level}
                className={`${LEVEL_FILL[level]} h-full transition-[width] duration-500`}
                style={{ width: `${(risk.totals[level] / total) * 100}%` }}
                title={`${level}: ${risk.totals[level]}`}
              />
            ) : null,
          )}
        </div>
        <span className="hidden shrink-0 text-xs text-fg-subtle sm:inline">
          {risk.at_risk} of {risk.shipments.length} shipments at risk
        </span>
      </div>
    </div>
  );
}

function ShipmentRiskRow({ shipment, onAsk }: { shipment: RiskShipment; onAsk: (text: string) => void }) {
  const tone = levelTone[shipment.risk_level];
  return (
    <li className="flex gap-3 rounded-xl border border-line bg-surface p-3 animate-fade-up">
      <ScoreRing value={shipment.risk_score} max={100} tone={tone} size={46} label={shipment.risk_score} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] font-semibold text-fg">{shipment.shipment_id}</span>
          <LevelBadge level={shipment.risk_level} />
          <span className="text-xs text-fg-subtle">
            {shipment.truck_id} · {shipment.cargo_type}
          </span>
          <span className="ml-auto text-[11px] text-fg-subtle tabular-nums">ETA {shipment.eta_hours.toFixed(1)} h</span>
        </div>
        <div className="mt-0.5 flex items-center gap-1 truncate text-xs text-fg-muted">
          {shipment.origin_hub}
          <ArrowRight className="h-3 w-3 shrink-0 text-fg-subtle" aria-hidden />
          {shipment.destination_hub}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {shipment.factors.map((factor) => (
            <Pill key={factor.code} tone={factor.code === "temp_excursion" ? "danger" : factor.code === "telemetry_gap" ? "neutral" : "warn"} title={`+${factor.points} risk points`}>
              {factor.label}
            </Pill>
          ))}
          <button
            type="button"
            onClick={() =>
              onAsk(
                `${shipment.shipment_id} is ${shipment.risk_level} risk (${shipment.factors.map((f) => f.label).join("; ")}). What must dispatch do right now per the SOP, including live weather at the truck?`,
              )
            }
            className="ml-auto inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium text-accent hover:bg-accent/10"
          >
            <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden />
            Ask copilot
          </button>
        </div>
      </div>
    </li>
  );
}

function AffectedShipments({ risk, onAsk }: { risk: RiskOverview; onAsk: (text: string) => void }) {
  const [view, setView] = useState<"risk" | "critical" | "all">("risk");
  const rows = risk.shipments.filter((s) =>
    view === "all" ? true : view === "critical" ? s.risk_level === "critical" : s.risk_level !== "normal",
  );
  const options = [
    { id: "risk", label: `At risk · ${risk.at_risk}` },
    { id: "critical", label: `Critical · ${risk.totals.critical}` },
    { id: "all", label: `All · ${risk.shipments.length}` },
  ] as const;
  return (
    <Card className="flex flex-col">
      <CardHeader
        icon={Siren}
        title="Affected shipments"
        subtitle="Deterministic triage from SOP v4 thresholds · highest risk first"
        actions={
          <div className="flex rounded-lg border border-line bg-surface-2 p-0.5" role="group" aria-label="Filter by risk">
            {options.map((option) => (
              <button
                key={option.id}
                type="button"
                aria-pressed={view === option.id}
                onClick={() => setView(option.id)}
                className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                  view === option.id ? "bg-raised text-fg shadow-card" : "text-fg-subtle hover:text-fg"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        }
      />
      <div className="max-h-[640px] overflow-y-auto p-3">
        {rows.length === 0 ? (
          <EmptyState icon={ShieldCheck} title="Nothing at this level">
            No shipments match this risk filter right now.
          </EmptyState>
        ) : (
          <ul className="space-y-2">
            {rows.map((shipment) => (
              <ShipmentRiskRow key={shipment.shipment_id} shipment={shipment} onAsk={onAsk} />
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function DelayedShipments({ risk }: { risk: RiskOverview }) {
  const delayed = risk.shipments.filter((s) => s.shipment_status === "DELAYED_WEATHER");
  return (
    <Card>
      <CardHeader icon={CloudLightning} title="Delayed shipments" subtitle="Weather holds and their knock-on risk" />
      {delayed.length === 0 ? (
        <EmptyState icon={CloudLightning} title="No weather holds" />
      ) : (
        <ul className="divide-y divide-line">
          {delayed.map((s) => (
            <li key={s.shipment_id} className="flex items-start gap-3 px-4 py-3">
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${LEVEL_FILL[s.risk_level]}`} aria-hidden />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-semibold text-fg">{s.shipment_id}</span>
                  <span className="text-xs text-fg-subtle">{s.cargo_type}</span>
                  <span className="ml-auto text-xs font-medium text-warn tabular-nums">ETA {s.eta_hours.toFixed(1)} h</span>
                </div>
                <div className="mt-0.5 truncate text-xs text-fg-muted">
                  {s.origin_hub} → {s.destination_hub}
                </div>
                <div className="mt-1 text-[11px] text-fg-subtle">{s.factors.map((f) => f.label).join(" · ")}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function ImpactRows({ buckets }: { buckets: RiskBucket[] }) {
  return (
    <ul className="space-y-2.5 px-4 py-3">
      {buckets.slice(0, 8).map((bucket) => (
        <li key={bucket.name}>
          <div className="flex items-center gap-2 text-xs">
            <span className="min-w-0 flex-1 truncate font-medium text-fg">{bucket.name}</span>
            <span className="text-fg-subtle tabular-nums">
              {bucket.at_risk}/{bucket.shipments}
            </span>
            {bucket.at_risk ? <LevelBadge level={bucket.worst_level} /> : <Pill tone="ok">clear</Pill>}
          </div>
          <Meter
            className="mt-1.5"
            value={bucket.at_risk}
            max={bucket.shipments}
            tone={levelTone[bucket.worst_level] === "neutral" ? "ok" : levelTone[bucket.worst_level]}
            label={`${bucket.at_risk} of ${bucket.shipments} at risk`}
          />
          {bucket.cargo_types.length ? (
            <div className="mt-1 truncate text-[11px] text-fg-subtle">{bucket.cargo_types.join(", ")}</div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function SupplyChainImpact({ risk }: { risk: RiskOverview }) {
  const [group, setGroup] = useState<"destination" | "origin" | "cargo">("destination");
  const buckets = group === "destination" ? risk.by_destination : group === "origin" ? risk.by_origin : risk.by_cargo;
  return (
    <Card>
      <CardHeader
        icon={Network}
        title="Supply-chain impact"
        subtitle="Where at-risk freight is coming from and going to"
        actions={
          <select
            aria-label="Group impact by"
            value={group}
            onChange={(event) => setGroup(event.target.value as typeof group)}
            className="h-8 rounded-lg border border-line bg-surface-2 px-2 text-xs text-fg"
          >
            <option value="destination">By destination hub</option>
            <option value="origin">By origin hub</option>
            <option value="cargo">By cargo class</option>
          </select>
        }
      />
      <ImpactRows buckets={buckets} />
    </Card>
  );
}

export default function RiskDashboard({ onAsk }: { onAsk: (text: string) => void }) {
  const risk = usePolling(api.fleetRisk, 30_000);
  const data = risk.data;
  const freshest = data ? Math.min(...data.shipments.map((s) => s.minutes_since_ping)) : 0;

  return (
    <div className="space-y-5">
      {data && data.shipments.length && freshest > 20 ? (
        <div className="flex items-start gap-3 rounded-xl border border-warn/30 bg-warn/10 px-4 py-3 text-sm text-warn">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span>
            Telemetry is stale: the newest truck ping is {freshest} minutes old, so every shipment carries a telemetry-gap
            factor. The seed data has no live feed. Run <code className="font-mono">make ingest-db</code> to refresh it once,
            or set <code className="font-mono">TELEMETRY_SIMULATOR=true</code> to keep pings current.
          </span>
        </div>
      ) : null}

      {risk.error ? (
        <div className="rounded-xl border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">{risk.error}</div>
      ) : null}

      {data ? (
        <>
          <RiskSummary risk={data} />
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
            <AffectedShipments risk={data} onAsk={onAsk} />
            <div className="space-y-5">
              <DelayedShipments risk={data} />
              <SupplyChainImpact risk={data} />
            </div>
          </div>
        </>
      ) : !risk.error ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-24 rounded-2xl" />
          ))}
        </div>
      ) : null}
    </div>
  );
}
