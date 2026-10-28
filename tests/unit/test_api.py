"""HTTP contract of the FastAPI backend, with fake pools and scripted models (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout
from pydantic import SecretStr

from cold_chain_copilot.api.app import _mount_spa, create_app
from cold_chain_copilot.api.dependencies import (
    AppRuntime,
    ChatGuards,
    admin_limiter,
    get_runtime,
)
from cold_chain_copilot.api.schemas import ComponentStatus
from cold_chain_copilot.api.v1 import system
from cold_chain_copilot.api.v1.fleet import KPI_SQL, SHIPMENTS_SQL
from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.metrics import metrics
from tests.conftest import AgentFactory, ai, sql_call

ADMIN_PASSWORD = "correct-horse-battery-staple"
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

SHIPMENT_COLUMNS = [
    "shipment_id", "truck_id", "driver_name", "origin_hub", "destination_hub", "cargo_type",
    "current_lat", "current_lon", "reefer_temp_c", "target_min_temp_c", "target_max_temp_c",
    "door_status", "battery_pct", "eta_hours", "shipment_status", "last_updated_utc",
    "is_excursion", "excursion_c",
]  # fmt: skip
SHIPMENT_ROW = (
    "SHP-24004", "TRK-128", "Hannah Okafor", "Indianapolis, IN", "Atlanta, GA", "Biologics",
    Decimal("38.444132"), Decimal("-85.768678"), Decimal("9.60"), Decimal("2.00"),
    Decimal("8.00"), "OPEN", 75, Decimal("8.39"), "AT_HUB", NOW, True, Decimal("1.60"),
)  # fmt: skip
AUDIT_ROW = (
    7, "session-1", NOW, "tools", None, "query_fleet_telemetry",
    "SELECT 1 LIMIT 200", "ok", 42, None,
)  # fmt: skip


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], columns: list[str] | None = None) -> None:
        self._rows = rows
        self.description = [SimpleNamespace(name=c) for c in columns] if columns else None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    def execute(self, query: Any, params: Any = None) -> FakeCursor:
        if query == KPI_SQL:
            return FakeCursor([(25, 4, 2, 2, 1, NOW)])
        if query == SHIPMENTS_SQL:
            return FakeCursor([SHIPMENT_ROW], SHIPMENT_COLUMNS)
        if "count(*)" in query:
            return FakeCursor([(1,)])
        return FakeCursor([AUDIT_ROW])


class FakePool:
    def __init__(self, *, down: bool = False) -> None:
        self.down = down

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[FakeConnection]:
        if self.down:
            raise PoolTimeout("pool exhausted")
        yield FakeConnection()


class FakePools:
    def __init__(self, *, down: bool = False) -> None:
        self.ro = FakePool(down=down)
        self.admin = FakePool(down=down)

    def ping(self, pool: str = "ro", timeout: float = 3.0) -> bool:
        return not self.ro.down


class ListAudit:
    running = True
    pending = 0
    dropped = 0

    def __init__(self) -> None:
        self.events: list[Any] = []

    def log(self, event: Any) -> None:
        self.events.append(event)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        aiven_admin_db_uri=SecretStr("postgresql://avnadmin:adminpassword@db.example:5432/d"),
        aiven_agent_ro_db_uri=SecretStr("postgresql://usr_fde_ro:ropassword1234@db.example:5432/d"),
        deepseek_api_key=SecretStr("sk-test"),
        openai_api_key=SecretStr("sk-test"),
        pinecone_api_key=SecretStr("pc-test"),
        admin_audit_password=SecretStr(ADMIN_PASSWORD),
        frontend_dist=tmp_path / "no-dist",
        log_level="ERROR",
    )


@pytest.fixture
def client_factory(tmp_path: Path, make_agent: AgentFactory) -> Any:
    admin_limiter.reset("testclient")
    metrics.reset()

    def build(
        *,
        down: bool = False,
        flash: list[Any] | None = None,
        guards: ChatGuards | None = None,
    ) -> TestClient:
        settings = make_settings(tmp_path)
        app = create_app(settings)
        runtime = AppRuntime(
            settings=settings,
            pools=FakePools(down=down),  # type: ignore[arg-type]
            audit=ListAudit(),  # type: ignore[arg-type]
            copilot=make_agent(flash or [ai("Fleet is nominal.")]),
            guards=guards or ChatGuards.create(),
        )
        app.dependency_overrides[get_runtime] = lambda: runtime
        return TestClient(app)

    yield build
    admin_limiter.reset("testclient")


def test_health_reports_database_status(client_factory: Any) -> None:
    assert client_factory().get("/api/v1/health").json()["database"] == "ok"
    degraded = client_factory(down=True).get("/api/v1/health").json()
    assert degraded["status"] == "degraded"


def test_fleet_summary_returns_kpis_and_active_rows(client_factory: Any) -> None:
    response = client_factory().get("/api/v1/fleet/summary")
    assert response.status_code == 200
    body = response.json()
    assert (body["active_fleet"], body["temp_excursions"], body["weather_alerts"]) == (25, 4, 2)
    row = body["shipments"][0]
    assert row["shipment_id"] == "SHP-24004"
    assert row["is_excursion"] is True
    assert row["excursion_c"] == pytest.approx(1.6)
    assert "internal_margin_usd" not in row
    assert body["cost_savings"]["input_tokens_only"] is True
    assert response.headers["cache-control"] == "no-store"


def test_fleet_summary_is_503_when_database_is_down(client_factory: Any) -> None:
    response = client_factory(down=True).get("/api/v1/fleet/summary")
    assert response.status_code == 503


def test_chat_returns_answer_and_trace(client_factory: Any) -> None:
    flash = [
        ai(tool_calls=[sql_call("SELECT shipment_id FROM fde_views.vw_active_fleet", "c1")]),
        ai("2 shipments are out of band."),
    ]
    response = client_factory(flash=flash).post(
        "/api/v1/chat", json={"message": "Audit active temperature excursions"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "2 shipments are out of band."
    assert [step["node"] for step in body["trace_steps"]] == ["agent", "tools", "agent"]
    assert body["trace_steps"][1]["executed_sql"].endswith("LIMIT 200")
    assert len(body["session_id"]) >= 8


@pytest.mark.parametrize(
    "payload",
    [{"message": ""}, {"message": "hi", "session_id": "bad id!"}, {"message": "hi", "x": 1}],
)
def test_chat_rejects_invalid_requests(client_factory: Any, payload: dict[str, Any]) -> None:
    assert client_factory().post("/api/v1/chat", json=payload).status_code == 422


def test_audit_logs_require_admin_password(client_factory: Any) -> None:
    client = client_factory()
    assert client.post("/api/v1/audit/logs", json={}).status_code == 401
    wrong = client.post("/api/v1/audit/logs", json={}, headers={"X-Admin-Password": "nope"})
    assert wrong.status_code == 401

    ok = client.post("/api/v1/audit/logs", json={}, headers={"X-Admin-Password": ADMIN_PASSWORD})
    assert ok.status_code == 200
    body = ok.json()
    assert body["total"] == 1
    assert body["entries"][0]["tool_called"] == "query_fleet_telemetry"


def test_admin_lockout_after_repeated_failures(client_factory: Any) -> None:
    client = client_factory()
    for _ in range(5):
        client.post("/api/v1/audit/logs", json={}, headers={"X-Admin-Password": "wrong"})
    locked = client.post(
        "/api/v1/audit/logs", json={}, headers={"X-Admin-Password": ADMIN_PASSWORD}
    )
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


def test_rbac_test_requires_admin_password(client_factory: Any) -> None:
    assert client_factory().post("/api/v1/audit/rbac-test").status_code == 401


def test_chat_rate_limit_returns_429_before_any_model_call(client_factory: Any) -> None:
    client = client_factory(
        flash=[ai("one"), ai("two"), ai("three")], guards=ChatGuards.create(per_minute=2)
    )
    for _ in range(2):
        assert client.post("/api/v1/chat", json={"message": "hi"}).status_code == 200
    limited = client.post("/api/v1/chat", json={"message": "hi"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert metrics.counts()["chat_rate_limited"] == 1
    assert metrics.counts()["chat_turns"] == 2


def test_daily_budget_blocks_new_turns(client_factory: Any) -> None:
    client = client_factory(
        flash=[ai("spent"), ai("never")], guards=ChatGuards.create(daily_budget_usd=0.0001)
    )
    assert client.post("/api/v1/chat", json={"message": "first"}).status_code == 200
    blocked = client.post("/api/v1/chat", json={"message": "second"})
    assert blocked.status_code == 429
    assert "budget" in blocked.json()["detail"]
    assert metrics.counts()["llm_budget_blocked"] == 1


def test_concurrency_cap_returns_503(client_factory: Any) -> None:
    guards = ChatGuards.create(max_concurrent=1)
    client = client_factory(guards=guards)
    assert guards.slots.acquire(blocking=False)
    try:
        busy = client.post("/api/v1/chat", json={"message": "hi"})
    finally:
        guards.slots.release()
    assert busy.status_code == 503
    assert client.post("/api/v1/chat", json={"message": "hi"}).status_code == 200


def test_fleet_risk_scores_and_groups(client_factory: Any) -> None:
    body = client_factory().get("/api/v1/fleet/risk").json()
    top = body["shipments"][0]
    assert top["shipment_id"] == "SHP-24004"
    assert top["risk_level"] == "critical"
    assert {f["code"] for f in top["factors"]} >= {"temp_excursion", "door_open"}
    assert body["totals"]["critical"] == 1
    assert body["by_destination"][0]["name"] == "Atlanta, GA"
    assert body["pharma_at_risk"] == 1


def test_system_status_reports_components_and_guardrails(
    client_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(key: str) -> Any:
        return lambda settings: ComponentStatus(key=key, name=key, status="ok", detail="fine")

    monkeypatch.setattr(system, "EXTERNAL_PROBES", {k: fake(k) for k in system.EXTERNAL_PROBES})
    system.probe_cache.clear()
    body = (
        client_factory()
        .get("/api/v1/system/status", headers={"X-Admin-Password": ADMIN_PASSWORD})
        .json()
    )

    assert body["status"] == "ok"
    keys = [c["key"] for c in body["components"]]
    assert keys[:2] == ["database_agent", "database_admin"]
    assert {"deepseek", "openai", "pinecone", "open_meteo", "audit_writer", "memory"} <= set(keys)
    guardrails = {g["key"]: g for g in body["guardrails"]}
    assert len(guardrails) == 18
    assert guardrails["chat_rate"]["triggered"] == 0
    assert guardrails["llm_budget"]["setting"] == "$5.00 per UTC day"
    assert "secret" not in str(body).lower()
    assert body["counters"]["chat_turns"] == 0
    system.probe_cache.clear()


def test_system_status_is_down_when_database_is_down(
    client_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system, "EXTERNAL_PROBES", {})
    client = client_factory(down=True)
    body = client.get("/api/v1/system/status", headers={"X-Admin-Password": ADMIN_PASSWORD}).json()
    assert body["status"] == "down"


def test_system_status_requires_admin_password(client_factory: Any) -> None:
    client = client_factory()
    assert client.get("/api/v1/system/status").status_code == 401
    wrong = client.get("/api/v1/system/status", headers={"X-Admin-Password": "nope"})
    assert wrong.status_code == 401


def test_spa_revalidates_html_and_never_serves_html_for_a_missing_asset(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script type="module" src="/assets/app-new.js"></script>')
    (dist / "assets" / "app-new.js").write_text("export {};")
    app = FastAPI()
    _mount_spa(app, dist)
    client = TestClient(app)

    for path in ("/", "/risk"):  # the page itself and a client-side route
        page = client.get(path)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-cache"
        assert "app-new.js" in page.text

    asset = client.get("/assets/app-new.js")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in asset.headers["content-type"]

    stale = client.get("/assets/app-old.js")  # requested by a page cached before a redeploy
    assert stale.status_code == 404
    assert "<script" not in stale.text
