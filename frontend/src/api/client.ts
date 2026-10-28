// Typed client for the FastAPI backend. Shapes mirror src/cold_chain_copilot/api/schemas.py.

export type ModelTier = "flash" | "pro";
export type EscalationReason = "sql_fix" | "synthesis";
export type StepStatus = "ok" | "error" | "blocked";
export type NodeName = "agent" | "tools" | "sql_fix" | "synthesize";

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cost_usd: number;
  all_pro_cost_usd: number;
  cache_saved_usd: number;
}

export interface TraceStep {
  step: number;
  node: NodeName;
  status: StepStatus;
  latency_ms: number;
  model: string | null;
  model_label: string | null;
  tier: ModelTier | null;
  escalation_reason: EscalationReason | null;
  tool_name: string | null;
  tool_input: Record<string, unknown> | null;
  executed_sql: string | null;
  payload: unknown;
  error_code: string | null;
  error: string | null;
  usage: TokenUsage | null;
  summary: string | null;
}

export interface ChatResponse {
  session_id: string;
  turn_id: string;
  answer: string;
  trace_steps: TraceStep[];
  models_used: string[];
  escalated: boolean;
  usage: TokenUsage;
  latency_ms: number;
}

export interface CostSavings {
  flash_calls: number;
  pro_calls: number;
  actual_cost_usd: number;
  all_pro_cost_usd: number;
  saved_usd: number;
  saved_pct: number;
  flash_share_pct: number;
  cache_hit_pct: number;
  cache_saved_usd: number;
  cache_priced: boolean;
  input_tokens_only: boolean;
}

export interface ActiveShipment {
  shipment_id: string;
  truck_id: string;
  driver_name: string;
  origin_hub: string;
  destination_hub: string;
  cargo_type: string;
  current_lat: number;
  current_lon: number;
  reefer_temp_c: number;
  target_min_temp_c: number;
  target_max_temp_c: number;
  door_status: string;
  battery_pct: number;
  eta_hours: number;
  shipment_status: string;
  last_updated_utc: string;
  is_excursion: boolean;
  excursion_c: number;
}

export interface FleetSummary {
  active_fleet: number;
  temp_excursions: number;
  weather_alerts: number;
  low_battery: number;
  doors_open: number;
  last_ping_utc: string | null;
  generated_at_utc: string;
  cost_savings: CostSavings;
  shipments: ActiveShipment[];
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  environment: string;
  database: "ok" | "unavailable";
}

export interface AuditLogQuery {
  limit?: number;
  offset?: number;
  session_id?: string | null;
  status?: string | null;
  node_name?: string | null;
}

export interface AuditLogEntry {
  log_id: number;
  session_id: string;
  timestamp_utc: string;
  node_name: string;
  model_used: string | null;
  tool_called: string | null;
  executed_sql: string | null;
  status: string;
  latency_ms: number | null;
  error_message: string | null;
}

export interface AuditLogPage {
  total: number;
  limit: number;
  offset: number;
  entries: AuditLogEntry[];
}

export type ProbeOutcome = "allowed" | "blocked";

export interface RbacProbeResult {
  probe_id: string;
  title: string;
  category: "read" | "write" | "ddl" | "privilege";
  sql: string;
  expected: ProbeOutcome;
  actual: ProbeOutcome;
  passed: boolean;
  sqlstate: string | null;
  detail: string;
}

export interface RbacTestReport {
  role: string;
  executed_at_utc: string;
  passed: boolean;
  total: number;
  passed_count: number;
  results: RbacProbeResult[];
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function describeDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) =>
        typeof item === "object" && item !== null && "msg" in item ? String(item.msg) : String(item),
      )
      .join("; ");
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit = {}, adminPassword?: string): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  if (adminPassword !== undefined) headers.set("X-Admin-Password", adminPassword);

  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Network error: the control tower API is unreachable.");
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body: unknown = await response.json();
      if (typeof body === "object" && body !== null && "detail" in body) {
        message = describeDetail(body.detail, message);
      }
    } catch {
      // Non-JSON error bodies keep the status-line message.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export const api = {
  health: (): Promise<HealthResponse> => request("/health"),

  fleetSummary: (): Promise<FleetSummary> => request("/fleet/summary"),

  chat: (message: string, sessionId: string | null): Promise<ChatResponse> =>
    request("/chat", {
      method: "POST",
      body: JSON.stringify({ message, session_id: sessionId }),
    }),

  auditLogs: (password: string, query: AuditLogQuery): Promise<AuditLogPage> =>
    request("/audit/logs", { method: "POST", body: JSON.stringify(query) }, password),

  rbacTest: (password: string): Promise<RbacTestReport> =>
    request("/audit/rbac-test", { method: "POST" }, password),
};
