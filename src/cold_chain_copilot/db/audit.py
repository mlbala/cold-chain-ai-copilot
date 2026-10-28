"""Non-blocking, append-only writer for `fde_views.agent_audit_log`.

Request handlers call `log()`, which only enqueues. A daemon thread drains the queue in
batches through the least-privilege pool (`usr_fde_ro` holds INSERT, nothing else on the
table), so audit writes never add latency to an agent turn and can never update or delete
history.
"""

from __future__ import annotations

import queue
import threading
from typing import Protocol

import psycopg
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from cold_chain_copilot.core.logging import get_logger

logger = get_logger(__name__)

_INSERT_SQL = """
INSERT INTO fde_views.agent_audit_log
    (session_id, node_name, model_used, tool_called, executed_sql, status, latency_ms,
     error_message)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_MAX_TEXT = 8000
# VARCHAR widths from 001_security_setup.sql; values are clipped, never rejected, so an
# oversized label can't make an agent turn fail.
_COLUMN_WIDTHS = {
    "session_id": 64,
    "node_name": 64,
    "status": 32,
    "model_used": 64,
    "tool_called": 64,
}


class AuditEvent(BaseModel):
    """One row of the audit trail. `log_id` and `timestamp_utc` are assigned by PostgreSQL."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    node_name: str
    status: str
    model_used: str | None = None
    tool_called: str | None = None
    executed_sql: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    error_message: str | None = None

    @field_validator(*_COLUMN_WIDTHS, mode="before")
    @classmethod
    def _fit_column(cls, value: object, info: ValidationInfo) -> object:
        width = _COLUMN_WIDTHS[info.field_name or ""]
        if isinstance(value, str) and len(value) > width:
            return value[: width - 3] + "..."
        return value

    def as_row(
        self,
    ) -> tuple[str, str, str | None, str | None, str | None, str, int | None, str | None]:
        return (
            self.session_id,
            self.node_name,
            self.model_used,
            self.tool_called,
            _clip(self.executed_sql),
            self.status,
            self.latency_ms,
            _clip(self.error_message),
        )


class AuditSink(Protocol):
    """Anything that accepts audit events without blocking the caller."""

    def log(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    """Discards events; used by offline tools and tests."""

    def log(self, event: AuditEvent) -> None:
        return None


class AuditLogger:
    """Background batch writer. Drops events (with a warning) rather than blocking."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        max_queue: int = 2000,
        batch_size: int = 50,
        flush_interval: float = 1.0,
    ) -> None:
        self._pool = pool
        self._queue: queue.Queue[AuditEvent | None] = queue.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._thread: threading.Thread | None = None
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Flush what is queued, then stop the writer thread."""
        if self._thread is None:
            return
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            logger.warning("audit_stop_queue_full")
        self._thread.join(timeout=timeout)
        self._thread = None

    def log(self, event: AuditEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            logger.warning("audit_event_dropped", reason="queue_full", node=event.node_name)

    def _run(self) -> None:
        stopping = False
        while not stopping:
            batch: list[AuditEvent] = []
            try:
                first = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                continue
            if first is None:
                stopping = True
            else:
                batch.append(first)
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    stopping = True
                    break
                batch.append(item)
            if batch:
                self._write(batch)

    def _write(self, batch: list[AuditEvent]) -> None:
        try:
            with self._pool.connection(timeout=5.0) as conn, conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, [event.as_row() for event in batch])
        except (psycopg.Error, TimeoutError) as exc:
            self._dropped += len(batch)
            logger.warning("audit_write_failed", events=len(batch), error=str(exc))


def _clip(value: str | None) -> str | None:
    if value is None or len(value) <= _MAX_TEXT:
        return value
    return value[: _MAX_TEXT - 3] + "..."
