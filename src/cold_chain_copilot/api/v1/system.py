"""GET /api/v1/system/status — component health, guardrail inventory, and live counters.

External dependencies are probed with free, read-only calls (model listings, index stats,
one forecast point) and cached for HEALTH_PROBE_TTL_SECONDS, so polling the dashboard never
spends tokens or hammers a provider. No secrets or connection details are returned.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter
from pinecone import Pinecone
from pinecone.exceptions import PineconeException

from cold_chain_copilot import __version__
from cold_chain_copilot.agent.tools.weather_meteo import OpenMeteoClient
from cold_chain_copilot.api.dependencies import RuntimeDep
from cold_chain_copilot.api.schemas import (
    ComponentState,
    ComponentStatus,
    GuardrailStatus,
    SystemStatus,
)
from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.core.metrics import CounterName, DependencyName, metrics

router = APIRouter(prefix="/system", tags=["ops"])
logger = get_logger(__name__)

_PROBE_TIMEOUT_S = 5.0
_PROBE_POINT = (35.1495, -90.0490)  # Memphis hub

Probe = Callable[[Settings], ComponentStatus]
_AGENT_DEPENDENCY: dict[str, DependencyName] = {
    "deepseek": "deepseek",
    "pinecone": "pinecone",
    "open_meteo": "open_meteo",
}


class _ProbeCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, ComponentStatus]] = {}

    def get(self, key: str, ttl: float, probe: Probe, settings: Settings) -> ComponentStatus:
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1]
        result = probe(settings)
        with self._lock:
            self._entries[key] = (time.monotonic(), result)
        return result

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


probe_cache = _ProbeCache()


def _status(
    key: str, name: str, state: ComponentState, detail: str, started: float | None = None
) -> ComponentStatus:
    return ComponentStatus(
        key=key,
        name=name,
        status=state,
        detail=detail,
        latency_ms=int((time.perf_counter() - started) * 1000) if started is not None else None,
        checked_at_utc=datetime.now(UTC),
    )


def _http_failure(key: str, name: str, exc: Exception, started: float) -> ComponentStatus:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        state: ComponentState = "down" if code in (401, 403) else "degraded"
        reason = "authentication failed" if code in (401, 403) else f"HTTP {code}"
        return _status(key, name, state, reason, started)
    return _status(key, name, "down", f"unreachable ({exc.__class__.__name__})", started)


def probe_deepseek(settings: Settings) -> ComponentStatus:
    key, name = "deepseek", "DeepSeek models"
    started = time.perf_counter()
    try:
        response = httpx.get(
            f"{settings.deepseek_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {settings.secret('deepseek_api_key')}"},
            timeout=_PROBE_TIMEOUT_S,
        )
        response.raise_for_status()
        available = {item.get("id") for item in response.json().get("data", [])}
    except (httpx.HTTPError, ValueError) as exc:
        return _http_failure(key, name, exc, started)
    wanted = [settings.deepseek_flash_model, settings.deepseek_pro_model]
    missing = [model for model in wanted if model not in available]
    if missing:
        offered = ", ".join(sorted(m for m in available if m)) or "none"
        detail = f"{', '.join(missing)} not listed by the API (offers: {offered})"
        return _status(key, name, "degraded", detail, started)
    return _status(key, name, "ok", f"{' + '.join(wanted)} available", started)


def probe_openai(settings: Settings) -> ComponentStatus:
    key, name = "openai", "OpenAI embeddings"
    started = time.perf_counter()
    try:
        response = httpx.get(
            f"https://api.openai.com/v1/models/{settings.openai_embedding_model}",
            headers={"Authorization": f"Bearer {settings.secret('openai_api_key')}"},
            timeout=_PROBE_TIMEOUT_S,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return _http_failure(key, name, exc, started)
    return _status(key, name, "ok", f"{settings.openai_embedding_model} available", started)


def probe_pinecone(settings: Settings) -> ComponentStatus:
    key, name = "pinecone", "Pinecone SOP index"
    started = time.perf_counter()
    try:
        client = Pinecone(api_key=settings.secret("pinecone_api_key"))
        if not client.describe_index(settings.pinecone_index_name).status.ready:
            return _status(key, name, "degraded", "index not ready", started)
        stats = client.Index(settings.pinecone_index_name).describe_index_stats()
    except PineconeException as exc:
        return _status(key, name, "down", f"unavailable ({exc.__class__.__name__})", started)
    namespace = stats.namespaces.get(settings.pinecone_namespace)
    vectors = namespace.vector_count if namespace else 0
    if vectors == 0:
        return _status(key, name, "degraded", "no SOP vectors; run ingest-sops", started)
    return _status(key, name, "ok", f"{vectors} SOP chunks indexed", started)


def probe_open_meteo(settings: Settings) -> ComponentStatus:
    key, name = "open_meteo", "Open-Meteo weather"
    started = time.perf_counter()
    client = OpenMeteoClient(
        settings.open_meteo_base_url, timeout_s=_PROBE_TIMEOUT_S, max_attempts=1
    )
    try:
        data = client.fetch(*_PROBE_POINT)
    except (httpx.HTTPError, ValueError) as exc:
        return _http_failure(key, name, exc, started)
    ok = "current" in data
    return _status(key, name, "ok" if ok else "degraded", "forecast API responding", started)


EXTERNAL_PROBES: dict[str, Probe] = {
    "deepseek": probe_deepseek,
    "openai": probe_openai,
    "pinecone": probe_pinecone,
    "open_meteo": probe_open_meteo,
}


def _database(runtime_ping: Callable[[], bool], key: str, name: str, role: str) -> ComponentStatus:
    started = time.perf_counter()
    ok = runtime_ping()
    return _status(key, name, "ok" if ok else "down", role if ok else "connection failed", started)


def _last_call_note(dependency: DependencyName) -> str:
    outcome = metrics.dependency(dependency)
    if outcome is None:
        return ""
    when = outcome.at.strftime("%H:%M UTC")
    return f"; last agent call {'ok' if outcome.ok else 'failed'} at {when}"


def guardrail_inventory(settings: Settings, max_sessions: int) -> list[GuardrailStatus]:
    counts = metrics.counts()

    def item(
        key: str,
        name: str,
        layer: str,
        setting: str,
        description: str,
        counter: CounterName | None = None,
    ) -> GuardrailStatus:
        return GuardrailStatus.model_validate(
            {
                "key": key,
                "name": name,
                "layer": layer,
                "setting": setting,
                "description": description,
                "triggered": counts[counter] if counter else None,
            }
        )

    budget = settings.llm_daily_budget_usd
    step_limit = 4 * (settings.agent_max_tool_calls + settings.sql_max_fix_attempts) + 10
    return [
        item(
            "least_privilege", "Least-privilege agent role", "database",
            "usr_fde_ro: SELECT on vw_active_fleet, INSERT on audit log",
            "PostgreSQL itself refuses every other read, write, DDL, SET ROLE and grant.",
            "rbac_violations",
        ),
        item(
            "margin_free_view", "Margin-free view", "database",
            "internal_margin_usd never projected; DELIVERED rows hidden",
            "Commercial data cannot reach the model, the UI or the logs.",
        ),
        item(
            "sql_guard", "SQL guard", "sql",
            "1 SELECT/WITH over fde_views.vw_active_fleet",
            "Tokenizer blocks DML/DDL, INTO, locking, pg_* and dblink functions, other schemas, "
            "stacked statements and prefixed literals before the database sees the query.",
            "sql_guard_blocks",
        ),
        item(
            "read_only_txn", "Read-only transaction", "sql",
            f"READ ONLY · statement_timeout {settings.sql_statement_timeout_ms} ms",
            "Every agent query runs in a read-only transaction with a hard time limit.",
        ),
        item(
            "row_cap", "Row cap", "sql", f"{settings.sql_row_limit} rows per query",
            "LIMIT is appended or clamped so a query can never return the whole table.",
        ),
        item(
            "sql_self_fix", "SQL self-correction cap", "agent",
            f"{settings.sql_max_fix_attempts} Pro repair attempts per turn",
            "Failed SQL is repaired by the Pro model, then the turn stops instead of guessing.",
            "sql_retry_exhausted",
        ),
        item(
            "tool_budget", "Per-turn tool budget", "agent",
            f"{settings.agent_max_tool_calls} tool calls per turn",
            "Extra tool calls are refused and the turn is synthesized from gathered evidence.",
            "tool_budget_exhausted",
        ),
        item(
            "step_limit", "Graph step limit", "agent", f"recursion limit {step_limit}",
            "A runaway agent loop ends with HTTP 422 instead of spinning.",
        ),
        item(
            "citations", "SOP citation contract", "agent",
            "policy claims must cite retrieved [SOP: …] excerpts",
            "The static system prompt forbids uncited thresholds, actions and deadlines.",
        ),
        item(
            "session_cap", "Conversation memory cap", "agent",
            f"{max_sessions} sessions (least recently used evicted)",
            "Bounds in-memory checkpointer growth on the 1 GB host.",
        ),
        item(
            "chat_rate", "Per-client rate limit", "api",
            f"{settings.chat_rate_limit_per_minute} copilot turns per minute",
            "Excess requests get HTTP 429 with Retry-After before any model call.",
            "chat_rate_limited",
        ),
        item(
            "concurrency", "Concurrency cap", "api",
            f"{settings.chat_max_concurrent} simultaneous turns",
            "Protects memory and the database pool; overflow gets HTTP 503.",
            "chat_busy_rejected",
        ),
        item(
            "llm_budget", "Daily model budget", "api",
            f"${budget:.2f} per UTC day" if budget else "disabled",
            "New turns are refused once today's model spend reaches the cap.",
            "llm_budget_blocked",
        ),
        item(
            "admin_auth", "Admin password and lockout", "api",
            "constant-time check · 5 failures → 5 min lockout",
            "Gates the audit trail and the RBAC penetration test.",
            "admin_lockouts",
        ),
        item(
            "input_validation", "Strict input validation", "api",
            "message ≤ 4,000 chars · unknown fields rejected",
            "Pydantic schemas reject oversized or unexpected payloads with HTTP 422.",
        ),
        item(
            "trusted_proxies", "Trusted proxies", "api",
            f"X-Forwarded-For honoured only from {settings.forwarded_allow_ips}",
            "Clients cannot spoof their address to dodge rate limits or lockouts.",
        ),
        item(
            "security_headers", "Browser security headers", "api",
            "CSP · X-Frame-Options DENY · nosniff · no-store on /api",
            "Blocks script injection, framing and caching of API responses.",
        ),
        item(
            "append_only_audit", "Append-only audit trail", "audit",
            "UPDATE / DELETE / TRUNCATE rejected by trigger",
            "Every model and tool call is logged; not even avnadmin can alter history.",
        ),
    ]  # fmt: skip


def _overall(components: list[ComponentStatus]) -> ComponentState:
    if any(c.key.startswith("database") and c.status == "down" for c in components):
        return "down"
    if any(c.status in ("down", "degraded") for c in components):
        return "degraded"
    return "ok"


@router.get("/status", response_model=SystemStatus, summary="Health, guardrails, and counters")
def system_status(runtime: RuntimeDep) -> SystemStatus:
    settings = runtime.settings
    ttl = settings.health_probe_ttl_seconds

    with ThreadPoolExecutor(max_workers=len(EXTERNAL_PROBES) + 2) as pool:
        external = {
            key: pool.submit(probe_cache.get, key, ttl, probe, settings)
            for key, probe in EXTERNAL_PROBES.items()
        }
        agent_db = pool.submit(
            _database,
            lambda: runtime.pools.ping("ro", timeout=2.0),
            "database_agent",
            "PostgreSQL (agent role)",
            "usr_fde_ro connected",
        )
        admin_db = pool.submit(
            _database,
            lambda: runtime.pools.ping("admin", timeout=2.0),
            "database_admin",
            "PostgreSQL (admin role)",
            "avnadmin connected",
        )
        components = [agent_db.result(), admin_db.result()]
        for key, future in external.items():
            status = future.result()
            dependency = _AGENT_DEPENDENCY.get(key)
            note = _last_call_note(dependency) if dependency else ""
            components.append(status.model_copy(update={"detail": status.detail + note}))

    audit = runtime.audit
    components.append(
        _status(
            "audit_writer",
            "Audit writer",
            "ok" if audit.running and audit.dropped == 0 else "degraded",
            f"{audit.pending} queued · {audit.dropped} dropped"
            + ("" if audit.running else " · writer stopped"),
        )
    )
    copilot = runtime.copilot
    components.append(
        _status(
            "memory",
            "Conversation memory",
            "ok",
            f"{copilot.active_sessions} of {copilot.max_sessions} sessions",
        )
    )

    now = datetime.now(UTC)
    return SystemStatus(
        status=_overall(components),
        version=__version__,
        environment=settings.app_env,
        started_at_utc=metrics.started_at,
        uptime_s=int((now - metrics.started_at).total_seconds()),
        generated_at_utc=now,
        components=components,
        guardrails=guardrail_inventory(settings, copilot.max_sessions),
        counters={str(name): value for name, value in metrics.counts().items()},
        llm_spend_today_usd=round(copilot.router.ledger.spent_today(), 6),
        llm_daily_budget_usd=settings.llm_daily_budget_usd,
        active_sessions=copilot.active_sessions,
        max_sessions=copilot.max_sessions,
    )
