"""FastAPI dependencies: runtime services, settings, pools, guardrails, and admin auth."""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from cold_chain_copilot.agent.graph import CopilotAgent
from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.core.metrics import metrics
from cold_chain_copilot.db.audit import AuditLogger
from cold_chain_copilot.db.pool import DatabasePools

logger = get_logger(__name__)


class SlidingWindowLimiter:
    """Per-client event counts over a sliding time window (thread-safe, memory-bounded)."""

    def __init__(self, max_events: int, window_s: float) -> None:
        self.max_events = max_events
        self.window_s = window_s
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, client: str, now: float) -> deque[float]:
        events = self._events[client]
        while events and now - events[0] > self.window_s:
            events.popleft()
        return events

    def _retry_after(self, events: deque[float], now: float) -> int | None:
        if len(events) < self.max_events:
            return None
        return int(self.window_s - (now - events[0])) + 1

    def retry_after(self, client: str) -> int | None:
        """Seconds until `client` is allowed again, or None when under the limit."""
        now = time.monotonic()
        with self._lock:
            retry = self._retry_after(self._prune(client, now), now)
            if not self._events[client]:
                del self._events[client]
            return retry

    def record(self, client: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(client, now).append(now)

    def try_acquire(self, client: str) -> int | None:
        """Atomically check and record one event; returns Retry-After seconds if limited."""
        now = time.monotonic()
        with self._lock:
            events = self._prune(client, now)
            retry = self._retry_after(events, now)
            if retry is None:
                events.append(now)
            return retry

    def reset(self, client: str) -> None:
        with self._lock:
            self._events.pop(client, None)


@dataclass(frozen=True, slots=True)
class ChatGuards:
    """API guardrails on /chat: per-client rate, concurrency cap, and daily model budget."""

    limiter: SlidingWindowLimiter
    slots: threading.BoundedSemaphore
    max_concurrent: int
    daily_budget_usd: float

    @classmethod
    def create(
        cls, *, per_minute: int = 12, max_concurrent: int = 4, daily_budget_usd: float = 5.0
    ) -> ChatGuards:
        return cls(
            limiter=SlidingWindowLimiter(per_minute, 60.0),
            slots=threading.BoundedSemaphore(max_concurrent),
            max_concurrent=max_concurrent,
            daily_budget_usd=daily_budget_usd,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> ChatGuards:
        return cls.create(
            per_minute=settings.chat_rate_limit_per_minute,
            max_concurrent=settings.chat_max_concurrent,
            daily_budget_usd=settings.llm_daily_budget_usd,
        )


@dataclass(frozen=True, slots=True)
class AppRuntime:
    """Long-lived services created in the lifespan and shared by every request."""

    settings: Settings
    pools: DatabasePools
    audit: AuditLogger
    copilot: CopilotAgent
    guards: ChatGuards = field(default_factory=ChatGuards.create)


def get_runtime(request: Request) -> AppRuntime:
    runtime: AppRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service is starting up")
    return runtime


RuntimeDep = Annotated[AppRuntime, Depends(get_runtime)]


def get_settings(runtime: RuntimeDep) -> Settings:
    return runtime.settings


def get_pools(runtime: RuntimeDep) -> DatabasePools:
    return runtime.pools


def get_copilot(runtime: RuntimeDep) -> CopilotAgent:
    return runtime.copilot


def get_audit(runtime: RuntimeDep) -> AuditLogger:
    return runtime.audit


def get_guards(runtime: RuntimeDep) -> ChatGuards:
    return runtime.guards


def client_id(request: Request) -> str:
    """Client address as seen after trusted-proxy resolution (see FORWARDED_ALLOW_IPS)."""
    return request.client.host if request.client else "unknown"


SettingsDep = Annotated[Settings, Depends(get_settings)]
PoolsDep = Annotated[DatabasePools, Depends(get_pools)]
CopilotDep = Annotated[CopilotAgent, Depends(get_copilot)]
AuditDep = Annotated[AuditLogger, Depends(get_audit)]
GuardsDep = Annotated[ChatGuards, Depends(get_guards)]


# Lock a client out after 5 wrong admin passwords within 5 minutes.
admin_limiter = SlidingWindowLimiter(max_events=5, window_s=300.0)


def require_admin(
    request: Request,
    settings: SettingsDep,
    x_admin_password: Annotated[str | None, Header(alias="X-Admin-Password")] = None,
) -> None:
    """Constant-time check of `X-Admin-Password` against `ADMIN_AUDIT_PASSWORD`."""
    client = client_id(request)
    retry_after = admin_limiter.retry_after(client)
    if retry_after is not None:
        metrics.increment("admin_lockouts")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed admin attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    expected = settings.secret("admin_audit_password")
    supplied = x_admin_password or ""
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        admin_limiter.record(client)
        metrics.increment("admin_auth_failures")
        logger.warning("admin_auth_failed", client=client)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin password")
    admin_limiter.reset(client)


AdminDep = Annotated[None, Depends(require_admin)]
