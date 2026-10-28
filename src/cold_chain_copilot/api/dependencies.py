"""FastAPI dependencies: runtime services, settings, pools, and admin authentication."""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from cold_chain_copilot.agent.graph import CopilotAgent
from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.db.audit import AuditLogger
from cold_chain_copilot.db.pool import DatabasePools

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AppRuntime:
    """Long-lived services created in the lifespan and shared by every request."""

    settings: Settings
    pools: DatabasePools
    audit: AuditLogger
    copilot: CopilotAgent


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


SettingsDep = Annotated[Settings, Depends(get_settings)]
PoolsDep = Annotated[DatabasePools, Depends(get_pools)]
CopilotDep = Annotated[CopilotAgent, Depends(get_copilot)]
AuditDep = Annotated[AuditLogger, Depends(get_audit)]


class FailedLoginLimiter:
    """Locks a client out after repeated wrong admin passwords inside a sliding window."""

    def __init__(self, max_failures: int = 5, window_s: float = 300.0) -> None:
        self._max_failures = max_failures
        self._window_s = window_s
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, client: str, now: float) -> deque[float]:
        attempts = self._failures[client]
        while attempts and now - attempts[0] > self._window_s:
            attempts.popleft()
        return attempts

    def retry_after(self, client: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            attempts = self._prune(client, now)
            if len(attempts) < self._max_failures:
                return None
            return int(self._window_s - (now - attempts[0])) + 1

    def record_failure(self, client: str) -> None:
        with self._lock:
            self._prune(client, time.monotonic()).append(time.monotonic())

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


admin_limiter = FailedLoginLimiter()


def require_admin(
    request: Request,
    settings: SettingsDep,
    x_admin_password: Annotated[str | None, Header(alias="X-Admin-Password")] = None,
) -> None:
    """Constant-time check of `X-Admin-Password` against `ADMIN_AUDIT_PASSWORD`."""
    client = request.client.host if request.client else "unknown"
    retry_after = admin_limiter.retry_after(client)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed admin attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    expected = settings.secret("admin_audit_password")
    supplied = x_admin_password or ""
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        admin_limiter.record_failure(client)
        logger.warning("admin_auth_failed", client=client)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin password")
    admin_limiter.reset(client)


AdminDep = Annotated[None, Depends(require_admin)]
