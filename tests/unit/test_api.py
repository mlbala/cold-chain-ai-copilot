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
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout
from pydantic import SecretStr

from cold_chain_copilot.api.app import create_app
from cold_chain_copilot.api.dependencies import AppRuntime, admin_limiter, get_runtime
from cold_chain_copilot.api.v1.fleet import KPI_SQL, SHIPMENTS_SQL
from cold_chain_copilot.core.config import Settings
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

    def build(*, down: bool = False, flash: list[Any] | None = None) -> TestClient:
        settings = make_settings(tmp_path)
        app = create_app(settings)
        runtime = AppRuntime(
            settings=settings,
            pools=FakePools(down=down),  # type: ignore[arg-type]
            audit=ListAudit(),  # type: ignore[arg-type]
            copilot=make_agent(flash or [ai("Fleet is nominal.")]),
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
