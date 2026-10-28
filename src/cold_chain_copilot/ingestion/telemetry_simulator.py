"""Demo telemetry heartbeat: keeps seed pings current when no live feed exists.

The seed shipments never report on their own, so after 20 minutes every truck would carry a
telemetry-gap risk factor. When `TELEMETRY_SIMULATOR=true`, this background thread refreshes
`last_updated_utc` for active shipments every `TELEMETRY_SIMULATOR_INTERVAL_SECONDS`, staggering
pings over the last few minutes. Temperatures, statuses, batteries, and ETAs are left alone, so
the scenario (25 active, 4 excursions, 2 weather delays) and its risk scores stay stable.

It writes through the admin pool: the agent role has no UPDATE privilege by design. Leave it
off wherever real telemetry flows into `public.legacy_fleet_shipments`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg_pool import ConnectionPool

from cold_chain_copilot.core.logging import get_logger

logger = get_logger(__name__)

HEARTBEAT_SQL = """
UPDATE public.legacy_fleet_shipments
SET last_updated_utc = now() - make_interval(secs => floor(random() * %(spread)s))
WHERE shipment_status <> 'DELIVERED'
"""


@dataclass(frozen=True, slots=True)
class HeartbeatStatus:
    enabled: bool
    interval_s: int
    last_tick_utc: datetime | None
    rows: int
    error: str | None


class TelemetrySimulator:
    """Background thread that refreshes ping timestamps on a fixed interval."""

    def __init__(self, pool: ConnectionPool, *, interval_s: int = 60, spread_s: int = 240) -> None:
        self._pool = pool
        self.interval_s = interval_s
        self._spread_s = spread_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_tick: datetime | None = None
        self._rows = 0
        self._error: str | None = None

    def tick(self) -> int:
        """One heartbeat. Returns rows updated; failures are recorded, never raised."""
        try:
            with self._pool.connection(timeout=10.0) as conn:
                rows = conn.execute(HEARTBEAT_SQL, {"spread": self._spread_s}).rowcount
        except (psycopg.Error, TimeoutError) as exc:
            with self._lock:
                self._error = f"{exc.__class__.__name__}: {exc}"
            logger.warning("telemetry_heartbeat_failed", error=str(exc))
            return 0
        with self._lock:
            self._last_tick = datetime.now(UTC)
            self._rows = rows
            self._error = None
        return rows

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telemetry-heartbeat", daemon=True)
        self._thread.start()
        logger.info("telemetry_simulator_started", interval_s=self.interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def status(self) -> HeartbeatStatus:
        with self._lock:
            return HeartbeatStatus(
                enabled=self._thread is not None and self._thread.is_alive(),
                interval_s=self.interval_s,
                last_tick_utc=self._last_tick,
                rows=self._rows,
                error=self._error,
            )

    def _run(self) -> None:
        self.tick()
        while not self._stop.wait(self.interval_s):
            self.tick()
