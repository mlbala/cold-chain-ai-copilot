"""Shared offline fixtures: fake DeepSeek models, fake tool backends, and an audit recorder."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from psycopg import errors as pg_errors

from cold_chain_copilot.agent.graph import CopilotAgent
from cold_chain_copilot.agent.router import HybridModelRouter, ModelSpec
from cold_chain_copilot.agent.tools.sop_pinecone import SopHit, build_sop_tool
from cold_chain_copilot.agent.tools.sql_telemetry import QueryResult, build_sql_tool
from cold_chain_copilot.agent.tools.weather_meteo import build_weather_tool
from cold_chain_copilot.db.audit import AuditEvent

FLASH_SPEC = ModelSpec(
    tier="flash",
    model_id="deepseek-flash",
    label="DeepSeek V4 Flash",
    input_price_per_m=0.15,
    output_price_per_m=0.0,
)
PRO_SPEC = ModelSpec(
    tier="pro",
    model_id="deepseek-v4-pro",
    label="DeepSeek V4 Pro",
    input_price_per_m=0.66,
    output_price_per_m=0.0,
)

USAGE = {"input_tokens": 2000, "output_tokens": 150, "total_tokens": 2150}


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """Scripted chat model; tool binding is a no-op because responses are pre-written."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ToolCallingFakeModel:
        return self


def ai(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [], usage_metadata=USAGE)


def sql_call(query: str, call_id: str) -> dict[str, Any]:
    return {"name": "query_fleet_telemetry", "args": {"query": query}, "id": call_id}


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def log(self, event: AuditEvent) -> None:
        self.events.append(event)


FLEET_ROWS = [
    {"shipment_id": "SHP-24004", "truck_id": "TRK-128", "reefer_temp_c": 9.6},
    {"shipment_id": "SHP-24009", "truck_id": "TRK-163", "reefer_temp_c": 4.3},
]


def fake_executor(query: str) -> QueryResult:
    """Behaves like PostgreSQL for the two cases the tests need."""
    if "bad_column" in query:
        raise pg_errors.UndefinedColumn('column "bad_column" does not exist')
    return QueryResult(
        columns=["shipment_id", "truck_id", "reefer_temp_c"], rows=FLEET_ROWS, truncated=False
    )


def fake_weather(latitude: float, longitude: float) -> dict[str, Any]:
    return {
        "current": {
            "time": "2026-10-28T09:00",
            "temperature_2m": 4.1,
            "relative_humidity_2m": 80,
            "precipitation": 0.4,
            "weather_code": 61,
            "wind_speed_10m": 31.0,
            "wind_gusts_10m": 58.0,
        },
        "hourly": {
            "time": [f"2026-10-28T{h:02d}:00" for h in range(9, 15)],
            "temperature_2m": [4.1, 3.8, 3.5, 3.0, 2.8, 2.5],
            "precipitation": [0.4, 0.9, 1.2, 0.3, 0.0, 0.0],
            "precipitation_probability": [70, 80, 85, 40, 20, 10],
            "wind_gusts_10m": [58.0, 66.0, 71.0, 52.0, 40.0, 33.0],
        },
    }


def fake_sop_search(query: str, top_k: int) -> list[SopHit]:
    return [
        SopHit(
            vector_id="cold_chain_sop_v4.md#abc",
            source_file="cold_chain_sop_v4.md",
            section="4. Temperature Excursion Response",
            score=0.87,
            text="Severity 2 (Major): any pharma-grade excursion...",
        )
    ][:top_k]


@pytest.fixture
def tools() -> list[BaseTool]:
    return [
        build_sql_tool(fake_executor, row_limit=200),
        build_weather_tool(fake_weather),
        build_sop_tool(fake_sop_search),
    ]


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


AgentFactory = Callable[..., CopilotAgent]


@pytest.fixture
def make_agent(tools: list[BaseTool], audit: RecordingAudit) -> AgentFactory:
    def factory(
        flash: list[AIMessage],
        pro: list[AIMessage] | None = None,
        *,
        max_fix_attempts: int = 2,
        max_tool_calls: int = 6,
    ) -> CopilotAgent:
        router = HybridModelRouter(
            flash=ToolCallingFakeModel(responses=flash),
            pro=ToolCallingFakeModel(responses=pro or [ai("unused")]),
            flash_spec=FLASH_SPEC,
            pro_spec=PRO_SPEC,
            tools=tools,
            max_fix_attempts=max_fix_attempts,
            max_tool_calls=max_tool_calls,
        )
        return CopilotAgent(router=router, tools=tools, audit=audit)

    return factory
