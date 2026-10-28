"""LangGraph StateGraph for one dispatcher turn, with MemorySaver conversation memory.

    START -> agent --tool calls--> tools --SQL failure--> sql_fix --fixed SQL--> tools
               |                     |                       |
               |                     +--budget spent--> synthesize --> END
               +--multi-source draft--> synthesize            +--retries exhausted--> END
               +--final answer--> END

`agent` runs on Flash; `sql_fix` and `synthesize` run on Pro (see `router.py`). Every node
appends a `TraceStep` for the UI and writes an audit event to `fde_views.agent_audit_log`.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Any, Literal
from uuid import uuid4

import structlog
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, ValidationError

from cold_chain_copilot.agent.router import HybridModelRouter, LLMCall
from cold_chain_copilot.agent.state import (
    AgentState,
    EscalationReason,
    NodeName,
    SqlFailure,
    StepStatus,
    TokenUsage,
    TraceStep,
    turn_inputs,
)
from cold_chain_copilot.agent.tools import (
    SOP_TOOL_NAME,
    SQL_TOOL_NAME,
    WEATHER_TOOL_NAME,
    ToolOutcome,
)
from cold_chain_copilot.agent.tools.sop_pinecone import SopRetriever, build_sop_tool
from cold_chain_copilot.agent.tools.sql_telemetry import build_sql_tool, pool_executor
from cold_chain_copilot.agent.tools.weather_meteo import OpenMeteoClient, build_weather_tool
from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.exceptions import SQLRetryExhaustedError
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.db.audit import AuditEvent, AuditSink

logger = get_logger(__name__)

_ORIGIN_KEY = "fde_origin"
_EVIDENCE_CHAR_LIMIT = 6000
_SQL_BLOCK_RE = re.compile(r"```(?:sql|postgresql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """The static, prefix-cacheable system prompt shipped inside the package."""
    return (
        resources.files("cold_chain_copilot.agent")
        .joinpath("prompts", "system_prompt.md")
        .read_text(encoding="utf-8")
        .strip()
    )


def extract_sql(text: str) -> str | None:
    """Pull one SQL statement out of a model reply (fenced block preferred)."""
    match = _SQL_BLOCK_RE.search(text)
    candidate = (match.group(1) if match else text).strip().rstrip(";").strip()
    if re.match(r"^(select|with)\b", candidate, re.IGNORECASE):
        return candidate
    return None


def _recent_history(messages: Sequence[AnyMessage], turns: int) -> list[AnyMessage]:
    """Messages from the start of the last `turns` human turns, with orphaned tool traffic
    removed (an interrupted turn can leave tool calls without results)."""
    human_positions = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    start = human_positions[-turns] if len(human_positions) >= turns else 0
    window = list(messages[start:])

    answered = {m.tool_call_id for m in window if isinstance(m, ToolMessage)}
    kept: list[AnyMessage] = []
    valid_calls: set[str] = set()
    for message in window:
        if isinstance(message, AIMessage) and message.tool_calls:
            ids = {call["id"] or "" for call in message.tool_calls}
            if not ids <= answered:
                continue
            valid_calls |= ids
        if isinstance(message, ToolMessage) and message.tool_call_id not in valid_calls:
            continue
        kept.append(message)
    return kept


def _turn_evidence(messages: Sequence[AnyMessage]) -> list[ToolMessage]:
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    return [m for m in messages[last_human + 1 :] if isinstance(m, ToolMessage)]


def _summarize_outcome(outcome: ToolOutcome) -> str:
    if outcome.status != "ok":
        return outcome.error_code or outcome.status
    payload = outcome.payload if isinstance(outcome.payload, dict) else {}
    if outcome.tool_name == SQL_TOOL_NAME:
        suffix = " (truncated)" if payload.get("truncated") else ""
        return f"{payload.get('row_count', 0)} row(s){suffix}"
    if outcome.tool_name == WEATHER_TOOL_NAME:
        current = payload.get("current") or {}
        return (
            f"{payload.get('location')}: {current.get('conditions') or 'n/a'}, "
            f"gusts {current.get('wind_gusts_kmh')} km/h"
        )
    if outcome.tool_name == SOP_TOOL_NAME:
        return f"{len(payload.get('hits') or [])} SOP excerpt(s)"
    return "ok"


class TurnResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    turn_id: str
    answer: str
    trace_steps: list[TraceStep]
    models_used: list[str]
    escalated: bool
    usage: TokenUsage
    latency_ms: int


class CopilotAgent:
    """Builds the compiled graph and runs dispatcher turns against it."""

    def __init__(
        self,
        *,
        router: HybridModelRouter,
        tools: Sequence[BaseTool],
        audit: AuditSink,
        history_turns: int = 4,
        max_sessions: int = 500,
    ) -> None:
        self.router = router
        self._tools = {t.name: t for t in tools}
        self._audit = audit
        self._history_turns = history_turns
        self._max_sessions = max_sessions
        self._checkpointer = InMemorySaver()
        self._sessions: OrderedDict[str, threading.Lock] = OrderedDict()
        self._sessions_lock = threading.Lock()
        self.graph = self._build()

    # --- Graph wiring ---------------------------------------------------------------------

    def _build(self) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
        builder: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
        builder.add_node("agent", self._agent_node)
        builder.add_node("tools", self._tools_node)
        builder.add_node("sql_fix", self._sql_fix_node)
        builder.add_node("synthesize", self._synthesize_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", self._after_agent, ["tools", "synthesize", END])
        builder.add_conditional_edges(
            "tools", self.router.route_after_tools, ["agent", "sql_fix", "synthesize"]
        )
        builder.add_conditional_edges("sql_fix", self._after_sql_fix, ["tools", "sql_fix", END])
        builder.add_edge("synthesize", END)
        return builder.compile(checkpointer=self._checkpointer)

    @staticmethod
    def _after_agent(state: AgentState) -> Literal["tools", "synthesize", "__end__"]:
        if state["draft_answer"] is not None:
            return "synthesize"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "__end__"

    @staticmethod
    def _after_sql_fix(state: AgentState) -> Literal["tools", "sql_fix", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        if state["pending_sql_failure"] is not None:
            return "sql_fix"
        return "__end__"

    # --- Nodes ----------------------------------------------------------------------------

    def _agent_node(self, state: AgentState) -> dict[str, Any]:
        system = SystemMessage(
            content=f"{load_system_prompt()}\n\n# Session context\n"
            f"Current time (UTC): {datetime.now(UTC):%Y-%m-%d %H:%M}"
        )
        messages: list[BaseMessage] = [
            system,
            *_recent_history(state["messages"], self._history_turns),
        ]
        call = self._llm_step(state, "agent", None, lambda: self.router.call_flash(messages))
        response = call.message
        escalate = not response.tool_calls and self.router.needs_synthesis(state)
        summary = None if response.tool_calls else "Draft answer (escalating)" if escalate else None
        steps = [
            *state["trace_steps"],
            self._llm_trace(state, call, "agent", None, summary=summary),
        ]
        if escalate:
            return {"draft_answer": response.text, "trace_steps": steps}
        return {"messages": [response], "trace_steps": steps}

    def _tools_node(self, state: AgentState) -> dict[str, Any]:
        request = state["messages"][-1]
        if not isinstance(request, AIMessage):
            raise TypeError("tools node expects an AIMessage with tool calls")
        origin = request.response_metadata.get(_ORIGIN_KEY)
        escalation: EscalationReason | None = "sql_fix" if origin == "sql_fix" else None

        steps = list(state["trace_steps"])
        tools_used = list(state["tools_used"])
        replies: list[ToolMessage] = []
        failure: SqlFailure | None = None
        budget = self.router.remaining_tool_budget(state)
        executed = 0

        for call in request.tool_calls:
            if executed >= budget:
                outcome = ToolOutcome(
                    tool_name=call["name"],
                    status="error",
                    content="SKIPPED: the per-turn tool budget is exhausted; answer with the "
                    "evidence already gathered.",
                    error_code="UPSTREAM_ERROR",
                    error="tool budget exhausted",
                )
                latency_ms = 0
            else:
                started = time.perf_counter()
                outcome = self._execute_tool(call)
                latency_ms = int((time.perf_counter() - started) * 1000)
                executed += 1

            replies.append(
                ToolMessage(
                    content=outcome.content,
                    tool_call_id=call["id"] or "",
                    name=outcome.tool_name,
                    status="success" if outcome.status == "ok" else "error",
                )
            )
            if outcome.status == "ok" and outcome.tool_name not in tools_used:
                tools_used.append(outcome.tool_name)
            if outcome.is_fixable_sql_failure:
                failure = SqlFailure(
                    sql=outcome.executed_sql or str(call["args"].get("query", "")),
                    error=outcome.error or outcome.content,
                    error_code=outcome.error_code or "SQL_EXECUTION_ERROR",
                )
            elif outcome.tool_name == SQL_TOOL_NAME and outcome.status == "ok":
                failure = None

            steps.append(
                self._step(
                    steps,
                    node="tools",
                    status=outcome.status,
                    latency_ms=latency_ms,
                    escalation_reason=escalation,
                    tool_name=outcome.tool_name,
                    tool_input=dict(call["args"]),
                    executed_sql=outcome.executed_sql,
                    payload=outcome.payload,
                    error_code=outcome.error_code,
                    error=outcome.error,
                    summary=_summarize_outcome(outcome),
                )
            )
            self._audit.log(
                AuditEvent(
                    session_id=state["session_id"],
                    node_name="tools",
                    model_used=None,
                    tool_called=outcome.tool_name,
                    executed_sql=outcome.executed_sql,
                    status=outcome.error_code.lower()
                    if outcome.error_code == "RBAC_VIOLATION"
                    else outcome.status,
                    latency_ms=latency_ms,
                    error_message=outcome.error,
                )
            )

        return {
            "messages": replies,
            "trace_steps": steps,
            "tools_used": tools_used,
            "tool_calls_made": state["tool_calls_made"] + executed,
            "pending_sql_failure": failure,
        }

    def _sql_fix_node(self, state: AgentState) -> dict[str, Any]:
        failure = state["pending_sql_failure"]
        try:
            self.router.ensure_fix_budget(state)
        except SQLRetryExhaustedError as exc:
            steps = [
                *state["trace_steps"],
                self._step(
                    state["trace_steps"],
                    node="sql_fix",
                    status="error",
                    latency_ms=0,
                    model=self.router.pro_spec.model_id,
                    model_label=self.router.pro_spec.label,
                    tier="pro",
                    escalation_reason="sql_fix",
                    executed_sql=failure["sql"] if failure else None,
                    error_code="SQL_RETRY_EXHAUSTED",
                    error=str(exc),
                    summary=f"Gave up after {exc.attempts} repair attempt(s)",
                ),
            ]
            self._audit.log(
                AuditEvent(
                    session_id=state["session_id"],
                    node_name="sql_fix",
                    model_used=self.router.pro_spec.model_id,
                    tool_called=SQL_TOOL_NAME,
                    executed_sql=failure["sql"] if failure else None,
                    status="retry_exhausted",
                    latency_ms=0,
                    error_message=str(exc),
                )
            )
            apology = AIMessage(
                content=(
                    "I could not build a valid telemetry query for this question after "
                    f"{exc.attempts} automatic repair attempt(s), so I won't guess at the "
                    f"data. Last database error: {exc.last_error}\n\n"
                    "Try rephrasing with specific fields (for example shipment IDs, cargo "
                    "type, or status), or escalate to the NOC Shift Lead."
                ),
                response_metadata={_ORIGIN_KEY: "sql_fix"},
            )
            return {"messages": [apology], "pending_sql_failure": None, "trace_steps": steps}

        if failure is None:
            raise RuntimeError("sql_fix node reached without a pending SQL failure")
        prompt: list[BaseMessage] = [
            SystemMessage(content=load_system_prompt()),
            HumanMessage(
                content=(
                    "SQL self-correction task. The query below failed against "
                    "fde_views.vw_active_fleet.\n\n"
                    f"Dispatcher question: {state['question']}\n\n"
                    f"Failed SQL:\n```sql\n{failure['sql']}\n```\n"
                    f"Error code: {failure['error_code']}\n"
                    f"Error: {failure['error']}\n\n"
                    "Return exactly one corrected PostgreSQL query in a single ```sql fenced "
                    "block and nothing else. It must be one SELECT (or WITH ... SELECT) over "
                    "fde_views.vw_active_fleet only, using only columns listed in the data "
                    "contract."
                )
            ),
        ]
        call = self._llm_step(state, "sql_fix", "sql_fix", lambda: self.router.call_pro(prompt))
        attempts = state["sql_fix_attempts"] + 1
        fixed_sql = extract_sql(call.message.text)
        steps = [
            *state["trace_steps"],
            self._llm_trace(
                state,
                call,
                "sql_fix",
                "sql_fix",
                executed_sql=fixed_sql,
                summary=f"Repair attempt {attempts}" + ("" if fixed_sql else ": no SQL returned"),
            ),
        ]
        if fixed_sql is None:
            return {
                "sql_fix_attempts": attempts,
                "pending_sql_failure": SqlFailure(
                    sql=failure["sql"],
                    error="The repair model did not return a SQL statement.",
                    error_code=failure["error_code"],
                ),
                "trace_steps": steps,
            }
        retry_call = AIMessage(
            content="",
            tool_calls=[
                ToolCall(
                    name=SQL_TOOL_NAME,
                    args={"query": fixed_sql},
                    id=f"sqlfix_{uuid4().hex[:16]}",
                )
            ],
            response_metadata={
                _ORIGIN_KEY: "sql_fix",
                "model_name": self.router.pro_spec.model_id,
            },
        )
        return {
            "messages": [retry_call],
            "sql_fix_attempts": attempts,
            "pending_sql_failure": None,
            "trace_steps": steps,
        }

    def _synthesize_node(self, state: AgentState) -> dict[str, Any]:
        evidence = "\n\n".join(
            f"### {message.name or 'tool'} result\n{message.text[:_EVIDENCE_CHAR_LIMIT]}"
            for message in _turn_evidence(state["messages"])
        )
        prompt: list[BaseMessage] = [
            SystemMessage(content=load_system_prompt()),
            HumanMessage(
                content=(
                    "Synthesis task. Write the final answer to the dispatcher's question using "
                    "only the evidence below. Follow the answer format and the SOP citation "
                    "contract in your instructions; cite only SOP excerpts present below.\n\n"
                    f"Dispatcher question:\n{state['question']}\n\n"
                    f"Evidence gathered this turn:\n{evidence or '(no tool evidence)'}\n\n"
                    "Draft answer from the routing model (may be incomplete; correct and "
                    f"complete it):\n{state['draft_answer'] or '(none)'}"
                )
            ),
        ]
        call = self._llm_step(
            state, "synthesize", "synthesis", lambda: self.router.call_pro(prompt)
        )
        final = AIMessage(
            content=call.message.text,
            response_metadata={_ORIGIN_KEY: "synthesis", "model_name": call.spec.model_id},
        )
        steps = [*state["trace_steps"], self._llm_trace(state, call, "synthesize", "synthesis")]
        return {"messages": [final], "draft_answer": None, "trace_steps": steps}

    # --- Helpers --------------------------------------------------------------------------

    def _execute_tool(self, call: ToolCall) -> ToolOutcome:
        selected = self._tools.get(call["name"])
        if selected is None:
            return ToolOutcome(
                tool_name=call["name"],
                status="error",
                content=f"UNKNOWN_TOOL: {call['name']!r}. Available: {sorted(self._tools)}.",
                error_code="UNKNOWN_TOOL",
                error=f"unknown tool {call['name']}",
            )
        try:
            result = selected.invoke(call["args"])
        except ValidationError as exc:
            return ToolOutcome(
                tool_name=selected.name,
                status="error",
                content=f"INVALID_ARGUMENTS: {exc.errors(include_url=False)}",
                error_code="INVALID_ARGUMENTS",
                error=str(exc),
            )
        except Exception as exc:
            logger.exception("tool_failed", tool=selected.name)
            return ToolOutcome(
                tool_name=selected.name,
                status="error",
                content=f"UPSTREAM_ERROR: {selected.name} failed ({exc.__class__.__name__}).",
                error_code="UPSTREAM_ERROR",
                error=str(exc),
            )
        if isinstance(result, ToolOutcome):
            return result
        return ToolOutcome(tool_name=selected.name, status="ok", content=str(result))

    def _llm_step(
        self,
        state: AgentState,
        node: NodeName,
        escalation: EscalationReason | None,
        invoke: Callable[[], LLMCall],
    ) -> LLMCall:
        """Run one metered model call, auditing failures before re-raising them."""
        spec = self.router.flash_spec if escalation is None else self.router.pro_spec
        started = time.perf_counter()
        try:
            call = invoke()
        except Exception as exc:
            self._audit.log(
                AuditEvent(
                    session_id=state["session_id"],
                    node_name=node,
                    model_used=spec.model_id,
                    status="error",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_message=f"{exc.__class__.__name__}: {exc}",
                )
            )
            raise
        self._audit.log(
            AuditEvent(
                session_id=state["session_id"],
                node_name=node,
                model_used=call.spec.model_id,
                tool_called=",".join(dict.fromkeys(c["name"] for c in call.message.tool_calls))
                or None,
                status="ok",
                latency_ms=call.latency_ms,
            )
        )
        return call

    def _llm_trace(
        self,
        state: AgentState,
        call: LLMCall,
        node: NodeName,
        escalation: EscalationReason | None,
        *,
        executed_sql: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        names = [c["name"] for c in call.message.tool_calls]
        tool_calls = [{"name": c["name"], "args": c["args"]} for c in call.message.tool_calls]
        if summary is None:
            summary = f"Requested {', '.join(names)}" if tool_calls else "Final answer"
        return self._step(
            state["trace_steps"],
            node=node,
            status="ok",
            latency_ms=call.latency_ms,
            model=call.spec.model_id,
            model_label=call.spec.label,
            tier=call.spec.tier,
            escalation_reason=escalation,
            executed_sql=executed_sql,
            payload={"tool_calls": tool_calls} if tool_calls else None,
            usage=call.usage,
            summary=summary,
        )

    @staticmethod
    def _step(
        existing: Sequence[dict[str, Any]], *, node: NodeName, status: StepStatus, **fields: Any
    ) -> dict[str, Any]:
        return TraceStep(step=len(existing) + 1, node=node, status=status, **fields).model_dump(
            mode="json"
        )

    def _session_lock(self, session_id: str) -> threading.Lock:
        """Serialize turns per session and evict the least recently used conversations."""
        with self._sessions_lock:
            lock = self._sessions.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._sessions[session_id] = lock
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self._max_sessions:
                evicted, _ = self._sessions.popitem(last=False)
                self._checkpointer.delete_thread(evicted)
        return lock

    # --- Public API -----------------------------------------------------------------------

    def run_turn(self, message: str, session_id: str | None = None) -> TurnResult:
        session_id = session_id or uuid4().hex
        turn_id = uuid4().hex[:12]
        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": 4 * (self.router.max_tool_calls + self.router.max_fix_attempts) + 10,
        }
        started = time.perf_counter()
        with (
            structlog.contextvars.bound_contextvars(session_id=session_id, turn_id=turn_id),
            self._session_lock(session_id),
        ):
            final_state = self.graph.invoke(
                turn_inputs(session_id=session_id, turn_id=turn_id, question=message),
                config,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)

        steps = [TraceStep.model_validate(step) for step in final_state["trace_steps"]]
        answer = next(
            (
                m.text
                for m in reversed(final_state["messages"])
                if isinstance(m, AIMessage) and not m.tool_calls
            ),
            "",
        )
        usage = TokenUsage()
        models_used: list[str] = []
        for step in steps:
            if step.usage is not None:
                usage = usage + step.usage
            if step.model_label and step.model_label not in models_used:
                models_used.append(step.model_label)
        logger.info(
            "turn_completed",
            steps=len(steps),
            latency_ms=latency_ms,
            models=models_used,
            cost_usd=round(usage.cost_usd, 6),
        )
        return TurnResult(
            session_id=session_id,
            turn_id=turn_id,
            answer=answer,
            trace_steps=steps,
            models_used=models_used,
            escalated=any(step.tier == "pro" for step in steps),
            usage=usage,
            latency_ms=latency_ms,
        )


def build_copilot(settings: Settings, ro_pool: ConnectionPool, audit: AuditSink) -> CopilotAgent:
    """Wire the production tools, the hybrid router, and the graph."""
    executor = pool_executor(
        ro_pool,
        row_limit=settings.sql_row_limit,
        timeout_ms=settings.sql_statement_timeout_ms,
    )
    weather = OpenMeteoClient(
        settings.open_meteo_base_url, timeout_s=settings.weather_timeout_seconds
    )
    retriever = SopRetriever.from_settings(settings)
    tools = [
        build_sql_tool(executor, row_limit=settings.sql_row_limit),
        build_weather_tool(weather.fetch),
        build_sop_tool(
            lambda query, top_k: retriever.search(query, min(top_k, settings.sop_top_k))
        ),
    ]
    router = HybridModelRouter.from_settings(settings, tools)
    return CopilotAgent(
        router=router, tools=tools, audit=audit, history_turns=settings.history_turns
    )
