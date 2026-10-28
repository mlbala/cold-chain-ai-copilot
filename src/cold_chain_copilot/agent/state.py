"""Graph state and the trace-step model returned to the UI.

Trace steps live in graph state as plain dicts (`TraceStep.model_dump(mode="json")`) so the
checkpointer only ever serializes JSON-native values; the API re-validates them on the way out.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field

ModelTier = Literal["flash", "pro"]
EscalationReason = Literal["sql_fix", "synthesis"]
StepStatus = Literal["ok", "error", "blocked"]
NodeName = Literal["agent", "tools", "sql_fix", "synthesize"]


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    all_pro_cost_usd: float = 0.0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            all_pro_cost_usd=self.all_pro_cost_usd + other.all_pro_cost_usd,
        )


class TraceStep(BaseModel):
    """One observable step of an agent turn (an LLM call or a tool execution)."""

    model_config = ConfigDict(frozen=True)

    step: int = Field(ge=1)
    node: NodeName
    status: StepStatus
    latency_ms: int = Field(ge=0)
    model: str | None = None
    model_label: str | None = None
    tier: ModelTier | None = None
    escalation_reason: EscalationReason | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    executed_sql: str | None = None
    payload: Any = None
    error_code: str | None = None
    error: str | None = None
    usage: TokenUsage | None = None
    summary: str | None = None


class SqlFailure(TypedDict):
    """The last failing SQL call, handed to the Pro model for repair."""

    sql: str
    error: str
    error_code: str


class AgentState(TypedDict):
    """Per-thread graph state. Every non-message key is reset at the start of each turn."""

    messages: Annotated[list[AnyMessage], add_messages]
    session_id: str
    turn_id: str
    question: str
    trace_steps: list[dict[str, Any]]
    sql_fix_attempts: int
    tool_calls_made: int
    tools_used: list[str]
    pending_sql_failure: SqlFailure | None
    draft_answer: str | None


def turn_inputs(*, session_id: str, turn_id: str, question: str) -> AgentState:
    """Graph input for a new turn: one new human message plus freshly reset scratch state."""
    return AgentState(
        messages=[HumanMessage(content=question)],
        session_id=session_id,
        turn_id=turn_id,
        question=question,
        trace_steps=[],
        sql_fix_attempts=0,
        tool_calls_made=0,
        tools_used=[],
        pending_sql_failure=None,
        draft_answer=None,
    )
