"""Demo telemetry heartbeat: refreshes ping timestamps only, never raises."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import psycopg

from cold_chain_copilot.api.v1.system import _telemetry_feed
from cold_chain_copilot.ingestion.telemetry_simulator import HEARTBEAT_SQL, TelemetrySimulator


class FakePool:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, Any]] = []

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[Any]:
        if self.fail:
            raise psycopg.OperationalError("connection refused")
        pool = self

        class Conn:
            def execute(self, query: str, params: Any = None) -> SimpleNamespace:
                pool.calls.append((query, params))
                return SimpleNamespace(rowcount=25)

        yield Conn()


def test_tick_only_touches_ping_timestamps() -> None:
    pool = FakePool()
    simulator = TelemetrySimulator(pool, interval_s=60)  # type: ignore[arg-type]

    assert simulator.tick() == 25
    query, params = pool.calls[0]
    assert query == HEARTBEAT_SQL
    assert "SET last_updated_utc" in query
    assert "reefer_temp_c" not in query
    assert "shipment_status <> 'DELIVERED'" in query
    assert params == {"spread": 240}
    status = simulator.status()
    assert status.rows == 25
    assert status.last_tick_utc is not None
    assert status.error is None


def test_failed_tick_is_recorded_not_raised() -> None:
    simulator = TelemetrySimulator(FakePool(fail=True), interval_s=60)  # type: ignore[arg-type]
    assert simulator.tick() == 0
    assert "OperationalError" in (simulator.status().error or "")
    assert _telemetry_feed(simulator).status == "degraded"


def test_thread_ticks_immediately_and_stops() -> None:
    pool = FakePool()
    simulator = TelemetrySimulator(pool, interval_s=1)  # type: ignore[arg-type]
    simulator.start()
    deadline = time.monotonic() + 2
    while not pool.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert simulator.status().enabled
    simulator.stop()
    assert not simulator.status().enabled
    assert len(pool.calls) >= 1
    assert _telemetry_feed(simulator).status == "ok"


def test_feed_is_unknown_without_simulator() -> None:
    component = _telemetry_feed(None)
    assert component.status == "unknown"
    assert "TELEMETRY_SIMULATOR" in component.detail
