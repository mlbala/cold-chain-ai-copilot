"""Hybrid Flash/Pro routing: default Flash, Pro escalation for SQL repair and synthesis."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from cold_chain_copilot.agent.router import CostLedger, usage_from_message
from cold_chain_copilot.agent.state import TokenUsage, turn_inputs
from cold_chain_copilot.core.exceptions import SQLRetryExhaustedError
from tests.conftest import FLASH_SPEC, PRO_SPEC, AgentFactory, RecordingAudit, ai, sql_call

GOOD_SQL = "SELECT shipment_id, truck_id, reefer_temp_c FROM fde_views.vw_active_fleet"
BAD_SQL = "SELECT bad_column FROM fde_views.vw_active_fleet"


def _state(**overrides: Any) -> Any:
    state = dict(turn_inputs(session_id="s1", turn_id="t1", question="q"))
    state.update(overrides)
    return state


# --- Routing decisions --------------------------------------------------------------------


def test_routes_back_to_flash_after_successful_tools(make_agent: AgentFactory) -> None:
    router = make_agent([ai("x")]).router
    assert router.route_after_tools(_state(tool_calls_made=1)) == "agent"


def test_escalates_to_pro_on_sql_execution_error(make_agent: AgentFactory) -> None:
    router = make_agent([ai("x")]).router
    failure = {
        "sql": BAD_SQL,
        "error": "column does not exist",
        "error_code": "SQL_EXECUTION_ERROR",
    }
    assert router.route_after_tools(_state(pending_sql_failure=failure)) == "sql_fix"


def test_spent_tool_budget_goes_to_synthesis(make_agent: AgentFactory) -> None:
    router = make_agent([ai("x")], max_tool_calls=3).router
    assert router.route_after_tools(_state(tool_calls_made=3)) == "synthesize"


def test_fix_budget_raises_when_exhausted(make_agent: AgentFactory) -> None:
    router = make_agent([ai("x")], max_fix_attempts=2).router
    failure = {"sql": BAD_SQL, "error": "boom", "error_code": "SQL_EXECUTION_ERROR"}
    router.ensure_fix_budget(_state(sql_fix_attempts=1, pending_sql_failure=failure))
    with pytest.raises(SQLRetryExhaustedError) as excinfo:
        router.ensure_fix_budget(_state(sql_fix_attempts=2, pending_sql_failure=failure))
    assert excinfo.value.attempts == 2
    assert excinfo.value.last_error == "boom"


def test_synthesis_requires_two_distinct_tools(make_agent: AgentFactory) -> None:
    router = make_agent([ai("x")]).router
    assert not router.needs_synthesis(_state(tools_used=["query_fleet_telemetry"]))
    assert router.needs_synthesis(_state(tools_used=["query_fleet_telemetry", "search_sop"]))


def test_cost_ledger_reports_savings_against_all_pro() -> None:
    ledger = CostLedger()
    usage = usage_from_message(ai("x"), FLASH_SPEC, PRO_SPEC)
    ledger.record("flash", usage)
    snapshot = ledger.snapshot()
    assert usage.cost_usd == pytest.approx(2000 * 0.15 / 1e6)
    assert usage.all_pro_cost_usd == pytest.approx(2000 * 0.66 / 1e6)
    assert snapshot.saved_pct == pytest.approx(77.3, abs=0.1)
    assert snapshot.flash_share_pct == 100.0


# --- End-to-end turns on the compiled graph -----------------------------------------------


def test_flash_only_turn(make_agent: AgentFactory) -> None:
    agent = make_agent([ai(tool_calls=[sql_call(GOOD_SQL, "c1")]), ai("2 shipments found.")])
    result = agent.run_turn("Audit active temperature excursions")

    assert result.answer == "2 shipments found."
    assert [s.node for s in result.trace_steps] == ["agent", "tools", "agent"]
    assert result.models_used == ["DeepSeek V4 Flash"]
    assert not result.escalated
    assert result.trace_steps[1].executed_sql is not None
    assert result.usage.prompt_tokens == 4000


def test_sql_execution_error_escalates_to_pro_and_recovers(
    make_agent: AgentFactory, audit: RecordingAudit
) -> None:
    agent = make_agent(
        flash=[ai(tool_calls=[sql_call(BAD_SQL, "c1")]), ai("Recovered answer.")],
        pro=[ai(f"```sql\n{GOOD_SQL}\n```")],
    )
    result = agent.run_turn("Which trucks are out of band?")
    steps = result.trace_steps

    assert [s.node for s in steps] == ["agent", "tools", "sql_fix", "tools", "agent"]
    assert steps[1].error_code == "SQL_EXECUTION_ERROR"
    assert steps[2].tier == "pro"
    assert steps[2].escalation_reason == "sql_fix"
    assert steps[2].executed_sql == GOOD_SQL
    assert steps[3].status == "ok"
    assert steps[3].escalation_reason == "sql_fix"
    assert steps[4].tier == "flash"
    assert result.answer == "Recovered answer."
    assert result.escalated
    assert result.models_used == ["DeepSeek V4 Flash", "DeepSeek V4 Pro"]
    assert any(e.node_name == "sql_fix" and e.model_used == "deepseek-v4-pro" for e in audit.events)


def test_guard_block_also_escalates_to_pro(make_agent: AgentFactory) -> None:
    agent = make_agent(
        flash=[
            ai(tool_calls=[sql_call("SELECT * FROM public.legacy_fleet_shipments", "c1")]),
            ai("Done."),
        ],
        pro=[ai(f"```sql\n{GOOD_SQL}\n```")],
    )
    steps = agent.run_turn("Show raw shipments").trace_steps
    assert steps[1].status == "blocked"
    assert steps[1].error_code == "SQL_GUARD_BLOCKED"
    assert steps[2].node == "sql_fix"


def test_sql_retry_exhaustion_ends_turn_gracefully(
    make_agent: AgentFactory, audit: RecordingAudit
) -> None:
    agent = make_agent(
        flash=[ai(tool_calls=[sql_call(BAD_SQL, "c1")])],
        pro=[ai(f"```sql\n{BAD_SQL}\n```")],
        max_fix_attempts=2,
    )
    result = agent.run_turn("Broken question")
    nodes = [s.node for s in result.trace_steps]

    assert nodes == ["agent", "tools", "sql_fix", "tools", "sql_fix", "tools", "sql_fix"]
    assert result.trace_steps[-1].error_code == "SQL_RETRY_EXHAUSTED"
    assert "2 automatic repair attempt" in result.answer
    assert any(e.status == "retry_exhausted" for e in audit.events)


def test_multi_tool_turn_is_synthesized_by_pro(make_agent: AgentFactory) -> None:
    agent = make_agent(
        flash=[
            ai(
                tool_calls=[
                    sql_call(GOOD_SQL, "c1"),
                    {"name": "search_sop", "args": {"query": "biologics excursion"}, "id": "c2"},
                    {
                        "name": "get_live_weather",
                        "args": {"latitude": 38.44, "longitude": -85.77},
                        "id": "c3",
                    },
                ]
            ),
            ai("Draft from flash."),
        ],
        pro=[ai("Final synthesized answer [SOP: cold_chain_sop_v4.md].")],
    )
    result = agent.run_turn("Audit excursions and weather risk")
    steps = result.trace_steps

    assert [s.node for s in steps] == ["agent", "tools", "tools", "tools", "agent", "synthesize"]
    assert steps[-1].tier == "pro"
    assert steps[-1].escalation_reason == "synthesis"
    assert steps[4].summary == "Draft answer (escalating)"
    assert result.answer.startswith("Final synthesized answer")
    weather = steps[3].payload
    assert weather["next_6h"]["max_wind_gusts_kmh"] == 71.0


def test_tool_budget_is_enforced_per_turn(make_agent: AgentFactory) -> None:
    calls = [sql_call(GOOD_SQL, f"c{i}") for i in range(4)]
    agent = make_agent(
        flash=[ai(tool_calls=calls)],
        pro=[ai("Synthesized within budget.")],
        max_tool_calls=2,
    )
    result = agent.run_turn("Run many queries")
    tool_steps = [s for s in result.trace_steps if s.node == "tools"]
    assert sum(s.status == "ok" for s in tool_steps) == 2
    assert sum(s.error == "tool budget exhausted" for s in tool_steps) == 2
    assert result.trace_steps[-1].node == "synthesize"


def test_conversation_memory_persists_per_session(make_agent: AgentFactory) -> None:
    agent = make_agent([ai("First answer."), ai("Second answer.")])
    first = agent.run_turn("Hello", session_id="session-memory-1")
    second = agent.run_turn("And now?", session_id="session-memory-1")

    assert second.answer == "Second answer."
    assert first.turn_id != second.turn_id
    assert len(second.trace_steps) == 1
    snapshot = agent.graph.get_state({"configurable": {"thread_id": "session-memory-1"}})
    assert len(snapshot.values["messages"]) == 4


def test_token_usage_adds_up() -> None:
    total = TokenUsage(prompt_tokens=1, cost_usd=0.1) + TokenUsage(prompt_tokens=2, cost_usd=0.2)
    assert total.prompt_tokens == 3
    assert total.cost_usd == pytest.approx(0.3)


def test_cache_hits_bill_at_cache_price_only_when_configured() -> None:
    message = ai("x")
    message.usage_metadata = {
        "input_tokens": 2000,
        "output_tokens": 0,
        "total_tokens": 2000,
        "input_token_details": {"cache_read": 1800},
    }
    conservative = usage_from_message(message, FLASH_SPEC, PRO_SPEC)
    assert conservative.cached_tokens == 1800
    assert conservative.cost_usd == pytest.approx(2000 * 0.15 / 1e6)
    assert conservative.cache_saved_usd == pytest.approx(0.0)

    flash = replace(FLASH_SPEC, cached_input_price_per_m=0.015)
    pro = replace(PRO_SPEC, cached_input_price_per_m=0.066)
    priced = usage_from_message(message, flash, pro)
    assert priced.cost_usd == pytest.approx((200 * 0.15 + 1800 * 0.015) / 1e6)
    assert priced.all_pro_cost_usd == pytest.approx((200 * 0.66 + 1800 * 0.066) / 1e6)
    assert priced.cache_saved_usd == pytest.approx(1800 * (0.15 - 0.015) / 1e6)

    ledger = CostLedger()
    ledger.record("flash", priced)
    snapshot = ledger.snapshot()
    assert snapshot.cache_hit_pct == 90.0
    assert snapshot.cache_saved_usd == pytest.approx(priced.cache_saved_usd, abs=1e-6)


def test_deepseek_native_cache_hit_field_is_read() -> None:
    message = ai("x")
    message.response_metadata = {"token_usage": {"prompt_cache_hit_tokens": 1536}}
    assert usage_from_message(message, FLASH_SPEC, PRO_SPEC).cached_tokens == 1536


@pytest.mark.parametrize(
    ("mode", "body"),
    [
        ("disabled", {"thinking": {"type": "disabled"}}),
        ("enabled", {"thinking": {"type": "enabled"}}),
        ("provider_default", None),
    ],
)
def test_pro_thinking_switch(mode: str, body: dict[str, object] | None) -> None:
    from pydantic import SecretStr

    from cold_chain_copilot.agent.router import HybridModelRouter, pro_thinking_body
    from cold_chain_copilot.core.config import Settings

    assert pro_thinking_body(mode) == body
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        deepseek_api_key=SecretStr("sk-test"),
        deepseek_pro_thinking=mode,  # type: ignore[arg-type]
    )
    router = HybridModelRouter.from_settings(settings, tools=[])
    assert router._pro.extra_body == body  # type: ignore[attr-defined]
