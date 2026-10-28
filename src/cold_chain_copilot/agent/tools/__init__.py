"""Agent tools. Each tool returns a `ToolOutcome` so the graph can trace and audit it."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

SQL_TOOL_NAME = "query_fleet_telemetry"
WEATHER_TOOL_NAME = "get_live_weather"
SOP_TOOL_NAME = "search_sop"

ToolErrorCode = Literal[
    "SQL_GUARD_BLOCKED",
    "SQL_EXECUTION_ERROR",
    "RBAC_VIOLATION",
    "DATABASE_UNAVAILABLE",
    "INVALID_ARGUMENTS",
    "UPSTREAM_ERROR",
    "UNKNOWN_TOOL",
]

# SQL failures the Pro model can plausibly repair by rewriting the query.
FIXABLE_SQL_ERRORS: frozenset[str] = frozenset(
    {"SQL_GUARD_BLOCKED", "SQL_EXECUTION_ERROR", "RBAC_VIOLATION"}
)


class ToolOutcome(BaseModel):
    """What a tool hands back: text for the model, structured payload for the trace."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    status: Literal["ok", "error", "blocked"]
    content: str
    payload: Any = None
    executed_sql: str | None = None
    error_code: ToolErrorCode | None = None
    error: str | None = None

    @property
    def is_fixable_sql_failure(self) -> bool:
        return (
            self.tool_name == SQL_TOOL_NAME
            and self.status != "ok"
            and self.error_code in FIXABLE_SQL_ERRORS
        )


def to_jsonable(value: Any) -> Any:
    """Convert database and API values into JSON-native types."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    return value


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "FIXABLE_SQL_ERRORS",
    "SOP_TOOL_NAME",
    "SQL_TOOL_NAME",
    "WEATHER_TOOL_NAME",
    "ToolErrorCode",
    "ToolOutcome",
    "dumps",
    "to_jsonable",
]
