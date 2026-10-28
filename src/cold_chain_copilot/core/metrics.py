"""Process-lifetime guardrail counters and dependency outcomes for the health dashboard."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

CounterName = Literal[
    "chat_turns",
    "chat_errors",
    "chat_rate_limited",
    "chat_busy_rejected",
    "llm_budget_blocked",
    "sql_guard_blocks",
    "rbac_violations",
    "sql_repairs",
    "sql_retry_exhausted",
    "tool_budget_exhausted",
    "admin_auth_failures",
    "admin_lockouts",
]

COUNTER_NAMES: tuple[CounterName, ...] = (
    "chat_turns",
    "chat_errors",
    "chat_rate_limited",
    "chat_busy_rejected",
    "llm_budget_blocked",
    "sql_guard_blocks",
    "rbac_violations",
    "sql_repairs",
    "sql_retry_exhausted",
    "tool_budget_exhausted",
    "admin_auth_failures",
    "admin_lockouts",
)

DependencyName = Literal["deepseek", "pinecone", "open_meteo", "database"]


@dataclass(frozen=True, slots=True)
class DependencyOutcome:
    ok: bool
    at: datetime
    detail: str | None


class GuardrailMetrics:
    """Thread-safe counters. Values reset when the process restarts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[CounterName, int] = dict.fromkeys(COUNTER_NAMES, 0)
        self._dependencies: dict[DependencyName, DependencyOutcome] = {}
        self.started_at = datetime.now(UTC)

    def increment(self, name: CounterName, by: int = 1) -> None:
        with self._lock:
            self._counts[name] += by

    def record_dependency(self, name: DependencyName, ok: bool, detail: str | None = None) -> None:
        with self._lock:
            self._dependencies[name] = DependencyOutcome(ok=ok, at=datetime.now(UTC), detail=detail)

    def counts(self) -> dict[CounterName, int]:
        with self._lock:
            return dict(self._counts)

    def dependency(self, name: DependencyName) -> DependencyOutcome | None:
        with self._lock:
            return self._dependencies.get(name)

    def reset(self) -> None:
        with self._lock:
            self._counts = dict.fromkeys(COUNTER_NAMES, 0)
            self._dependencies.clear()
            self.started_at = datetime.now(UTC)


metrics = GuardrailMetrics()
