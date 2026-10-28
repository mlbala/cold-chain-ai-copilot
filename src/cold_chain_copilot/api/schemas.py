"""Pydantic v2 request and response envelopes for `/api/v1`."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cold_chain_copilot.agent.state import TokenUsage, TraceStep

SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(_Schema):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: Literal["ok", "unavailable"]


class ChatRequest(_Schema):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)


class ChatResponse(_Schema):
    session_id: str
    turn_id: str
    answer: str
    trace_steps: list[TraceStep]
    models_used: list[str]
    escalated: bool
    usage: TokenUsage
    latency_ms: int


class CostSavings(_Schema):
    flash_calls: int
    pro_calls: int
    actual_cost_usd: float
    all_pro_cost_usd: float
    saved_usd: float
    saved_pct: float
    flash_share_pct: float
    input_tokens_only: bool = Field(
        description="True when output-token prices are not configured, so costs cover input only."
    )


class ActiveShipment(_Schema):
    """One row of `fde_views.vw_active_fleet` (no commercial fields exist to expose)."""

    shipment_id: str
    truck_id: str
    driver_name: str
    origin_hub: str
    destination_hub: str
    cargo_type: str
    current_lat: float
    current_lon: float
    reefer_temp_c: float
    target_min_temp_c: float
    target_max_temp_c: float
    door_status: str
    battery_pct: int
    eta_hours: float
    shipment_status: str
    last_updated_utc: datetime
    is_excursion: bool
    excursion_c: float = Field(description="Degrees outside the target band (0 when in band).")


class FleetSummary(_Schema):
    active_fleet: int
    temp_excursions: int
    weather_alerts: int
    low_battery: int
    doors_open: int
    last_ping_utc: datetime | None
    generated_at_utc: datetime
    cost_savings: CostSavings
    shipments: list[ActiveShipment] = Field(
        description="Active rows, most urgent first (excursions, weather holds, low battery). "
        "Capped at SHIPMENT_ROW_LIMIT; compare with active_fleet to detect truncation."
    )


class AuditLogQuery(_Schema):
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=1_000_000)
    session_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    node_name: str | None = Field(default=None, max_length=64)


class AuditLogEntry(_Schema):
    log_id: int
    session_id: str
    timestamp_utc: datetime
    node_name: str
    model_used: str | None
    tool_called: str | None
    executed_sql: str | None
    status: str
    latency_ms: int | None
    error_message: str | None


class AuditLogPage(_Schema):
    total: int
    limit: int
    offset: int
    entries: list[AuditLogEntry]


ProbeCategory = Literal["read", "write", "ddl", "privilege"]
ProbeOutcome = Literal["allowed", "blocked"]


class RbacProbeResult(_Schema):
    probe_id: str
    title: str
    category: ProbeCategory
    sql: str
    expected: ProbeOutcome
    actual: ProbeOutcome
    passed: bool
    sqlstate: str | None
    detail: str


class RbacTestReport(_Schema):
    role: str
    executed_at_utc: datetime
    passed: bool
    total: int
    passed_count: int
    results: list[RbacProbeResult]
