import {
  ArrowRight,
  BatteryLow,
  CloudLightning,
  DoorOpen,
  MessageSquarePlus,
  Radio,
  Search,
  Thermometer,
  Truck,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useMemo, useState } from "react";

import type { ActiveShipment, FleetSummary } from "../api/client";
import type { PollState } from "../hooks/usePolling";
import { Card, CardHeader, EmptyState, Meter, Pill, Skeleton, timeAgo } from "./ui";
import type { Tone } from "./ui";

type Filter = "all" | "excursion" | "weather" | "battery" | "door";

const FILTERS: { id: Filter; label: string; icon: LucideIcon; test: (s: ActiveShipment) => boolean }[] = [
  { id: "all", label: "All", icon: Truck, test: () => true },
  { id: "excursion", label: "Excursions", icon: Thermometer, test: (s) => s.is_excursion },
  { id: "weather", label: "Weather", icon: CloudLightning, test: (s) => s.shipment_status === "DELAYED_WEATHER" },
  { id: "battery", label: "Low battery", icon: BatteryLow, test: (s) => s.battery_pct < 25 },
  { id: "door", label: "Door open", icon: DoorOpen, test: (s) => s.door_status === "OPEN" },
];

const STATUS_TONE: Record<string, Tone> = {
  IN_TRANSIT: "accent",
  AT_HUB: "pro",
  LOADING: "neutral",
  DELAYED_WEATHER: "warn",
};

function formatStatus(status: string): string {
  return status.replace("_", " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}

/** Target band with the current reading as a marker; the scale widens to fit excursions. */
function TempGauge({ shipment }: { shipment: ActiveShipment }) {
  const { target_min_temp_c: low, target_max_temp_c: high, reefer_temp_c: temp } = shipment;
  const pad = Math.max((high - low) * 0.6, shipment.excursion_c + 1.5);
  const start = Math.min(low - pad, temp - 0.5);
  const end = Math.max(high + pad, temp + 0.5);
  const position = (value: number) => ((value - start) / (end - start)) * 100;
  const out = shipment.is_excursion;
  return (
    <div className="flex items-center gap-3">
      <div className="relative h-2 flex-1 rounded-full bg-surface-3" aria-hidden>
        <div
          className="absolute inset-y-0 rounded-full bg-ok/25 ring-1 ring-ok/40"
          style={{ left: `${position(low)}%`, width: `${position(high) - position(low)}%` }}
        />
        <div
          className={`absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface ${out ? "bg-danger" : "bg-ok"}`}
          style={{ left: `${position(temp)}%` }}
        />
      </div>
      <span className="shrink-0 text-right text-xs whitespace-nowrap tabular-nums">
        <span className={`font-semibold ${out ? "text-danger" : "text-fg"}`}>{temp.toFixed(1)}°C</span>
        <span className="ml-1.5 text-[11px] text-fg-subtle">
          {low} to {high}
        </span>
      </span>
    </div>
  );
}

function ShipmentRow({ shipment, onAsk }: { shipment: ActiveShipment; onAsk: (text: string) => void }) {
  const lowBattery = shipment.battery_pct < 25;
  return (
    <li className="group animate-fade-up rounded-xl border border-line bg-surface p-3 transition-colors hover:border-line-strong hover:bg-surface-2/60">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="text-[13px] font-semibold text-fg">{shipment.shipment_id}</span>
            <span className="truncate text-xs text-fg-subtle">
              {shipment.truck_id} · <span className="font-medium text-fg-muted">{shipment.cargo_type}</span>
            </span>
          </div>
        </div>
        <Pill tone={STATUS_TONE[shipment.shipment_status] ?? "neutral"}>{formatStatus(shipment.shipment_status)}</Pill>
      </div>
      <div className="mt-1 flex min-w-0 items-center gap-1 text-xs text-fg-muted">
        <span className="truncate">{shipment.origin_hub}</span>
        <ArrowRight className="h-3 w-3 shrink-0 text-fg-subtle" aria-hidden />
        <span className="truncate">{shipment.destination_hub}</span>
      </div>

      <div className="mt-2.5">
        <TempGauge shipment={shipment} />
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-fg-subtle">
        <span className="flex w-24 items-center gap-1.5">
          <Meter value={shipment.battery_pct} tone={lowBattery ? "danger" : "ok"} label={`Battery ${shipment.battery_pct}%`} />
          <span className={`tabular-nums ${lowBattery ? "font-semibold text-danger" : ""}`}>{shipment.battery_pct}%</span>
        </span>
        {shipment.door_status === "OPEN" ? (
          <Pill tone="danger" icon={DoorOpen}>
            Door open
          </Pill>
        ) : null}
        <span className="tabular-nums">ETA {shipment.eta_hours.toFixed(1)} h</span>
        <span className="flex items-center gap-1">
          <Radio className="h-3 w-3" aria-hidden />
          {timeAgo(shipment.last_updated_utc)}
        </span>
        <button
          type="button"
          onClick={() =>
            onAsk(
              `Assess ${shipment.shipment_id} (${shipment.truck_id}, ${shipment.cargo_type}): check its telemetry, live weather at its position, and what the SOP requires right now.`,
            )
          }
          className="ml-auto inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-medium text-accent opacity-80 hover:bg-accent/10 hover:opacity-100 focus-visible:opacity-100"
        >
          <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden />
          Ask copilot
        </button>
      </div>
    </li>
  );
}

export default function FleetBoard({
  state,
  onAsk,
}: {
  state: PollState<FleetSummary>;
  onAsk: (text: string) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const shipments = state.data?.shipments ?? [];

  const counts = useMemo(
    () => Object.fromEntries(FILTERS.map((f) => [f.id, shipments.filter(f.test).length])) as Record<Filter, number>,
    [shipments],
  );

  const visible = useMemo(() => {
    const active = FILTERS.find((f) => f.id === filter) ?? FILTERS[0]!;
    const needle = query.trim().toLowerCase();
    return shipments.filter(
      (s) =>
        active.test(s) &&
        (!needle ||
          [s.shipment_id, s.truck_id, s.cargo_type, s.origin_hub, s.destination_hub, s.driver_name]
            .join(" ")
            .toLowerCase()
            .includes(needle)),
    );
  }, [shipments, filter, query]);

  return (
    <Card className="flex min-h-[420px] flex-col xl:sticky xl:top-[84px] xl:max-h-[calc(100dvh-104px)]">
      <CardHeader
        icon={Truck}
        title="Live fleet"
        subtitle={state.data ? `${shipments.length} active shipments · most urgent first` : "Loading telemetry…"}
      />
      <div className="space-y-2.5 border-b border-line px-4 py-3">
        <label className="relative block">
          <span className="sr-only">Search shipments</span>
          <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search ID, truck, cargo, hub, driver"
            className="h-8 w-full rounded-lg border border-line bg-surface-2 pr-3 pl-8 text-xs text-fg placeholder:text-fg-subtle focus:border-accent focus:outline-none"
          />
        </label>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter shipments">
          {FILTERS.map(({ id, label, icon: Icon }) => {
            const active = filter === id;
            return (
              <button
                key={id}
                type="button"
                aria-pressed={active}
                onClick={() => setFilter(id)}
                className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${
                  active
                    ? "border-accent/40 bg-accent/10 text-accent"
                    : "border-line text-fg-muted hover:border-line-strong hover:text-fg"
                }`}
              >
                <Icon className="h-3 w-3" aria-hidden />
                {label}
                <span className="tabular-nums opacity-70">{counts[id] ?? 0}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        {state.data === null ? (
          <div className="space-y-2">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-24 w-full rounded-xl" />
            ))}
          </div>
        ) : visible.length === 0 ? (
          <EmptyState icon={Search} title="No shipments match">
            Try another filter or clear the search.
          </EmptyState>
        ) : (
          <ul className="space-y-2">
            {visible.map((shipment) => (
              <ShipmentRow key={shipment.shipment_id} shipment={shipment} onAsk={onAsk} />
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
