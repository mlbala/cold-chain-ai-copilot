"""Hybrid DeepSeek V4 model router.

* Flash (`deepseek-chat`, $0.15 / 1M input tokens) handles tool routing, SQL drafting, and
  single-source answers: the large majority of calls.
* Pro (`deepseek-reasoner`, $0.66 / 1M input tokens) is used only when it pays for itself:
  - **sql_fix**: the last SQL call failed (guard block, RBAC denial, or execution error).
    Pro repairs the query, bounded by `sql_max_fix_attempts`.
  - **synthesis**: the turn gathered evidence from two or more distinct tools, so the final
    answer has to reconcile telemetry, weather, and policy.

Pro is never asked to call tools: it returns SQL text or a final answer, which keeps the
reasoning model off the tool-calling path entirely.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, SecretStr

from cold_chain_copilot.agent.state import AgentState, ModelTier, TokenUsage
from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.exceptions import SQLRetryExhaustedError

RouteAfterTools = Literal["agent", "sql_fix", "synthesize"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    tier: ModelTier
    model_id: str
    label: str
    input_price_per_m: float
    output_price_per_m: float
    cached_input_price_per_m: float | None = None

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
        """USD for one call. Cache-hit tokens bill at the cache price when one is configured,
        otherwise at the full input price (conservative)."""
        cached = min(max(cached_tokens, 0), prompt_tokens)
        cache_price = (
            self.input_price_per_m
            if self.cached_input_price_per_m is None
            else self.cached_input_price_per_m
        )
        return (
            (prompt_tokens - cached) * self.input_price_per_m
            + cached * cache_price
            + completion_tokens * self.output_price_per_m
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class LLMCall:
    message: AIMessage
    spec: ModelSpec
    usage: TokenUsage
    latency_ms: int


class LedgerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    flash_calls: int
    pro_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    actual_cost_usd: float
    all_pro_cost_usd: float
    saved_usd: float
    saved_pct: float
    flash_share_pct: float
    cache_hit_pct: float
    cache_saved_usd: float


class CostLedger:
    """Process-lifetime usage totals, used for the "Cost Savings" KPI tile."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[ModelTier, int] = {"flash": 0, "pro": 0}
        self._usage = TokenUsage()
        self._day: date = datetime.now(UTC).date()
        self._day_cost = 0.0

    def record(self, tier: ModelTier, usage: TokenUsage) -> None:
        with self._lock:
            self._calls[tier] += 1
            self._usage = self._usage + usage
            self._roll_day()
            self._day_cost += usage.cost_usd

    def spent_today(self) -> float:
        """Model spend (USD) since 00:00 UTC, for the daily budget guardrail."""
        with self._lock:
            self._roll_day()
            return self._day_cost

    def _roll_day(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day, self._day_cost = today, 0.0

    def snapshot(self) -> LedgerSnapshot:
        with self._lock:
            calls = dict(self._calls)
            usage = self._usage
        total_calls = calls["flash"] + calls["pro"]
        saved = max(usage.all_pro_cost_usd - usage.cost_usd, 0.0)
        return LedgerSnapshot(
            flash_calls=calls["flash"],
            pro_calls=calls["pro"],
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_tokens=usage.cached_tokens,
            actual_cost_usd=round(usage.cost_usd, 6),
            all_pro_cost_usd=round(usage.all_pro_cost_usd, 6),
            saved_usd=round(saved, 6),
            saved_pct=round(100 * saved / usage.all_pro_cost_usd, 1)
            if usage.all_pro_cost_usd
            else 0.0,
            flash_share_pct=round(100 * calls["flash"] / total_calls, 1) if total_calls else 0.0,
            cache_hit_pct=round(100 * usage.cached_tokens / usage.prompt_tokens, 1)
            if usage.prompt_tokens
            else 0.0,
            cache_saved_usd=round(usage.cache_saved_usd, 6),
        )


def usage_from_message(message: AIMessage, spec: ModelSpec, pro_spec: ModelSpec) -> TokenUsage:
    """Token counts and USD cost; DeepSeek reports cache hits as `prompt_cache_hit_tokens`."""
    metadata = message.usage_metadata
    prompt = int(metadata["input_tokens"]) if metadata else 0
    completion = int(metadata["output_tokens"]) if metadata else 0
    details = metadata.get("input_token_details") if metadata else None
    cached = int((details or {}).get("cache_read") or 0)
    if not cached:
        raw = message.response_metadata.get("token_usage") or {}
        cached = int(raw.get("prompt_cache_hit_tokens") or 0)
    cost = spec.cost(prompt, completion, cached)
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        cost_usd=cost,
        all_pro_cost_usd=pro_spec.cost(prompt, completion, cached),
        cache_saved_usd=spec.cost(prompt, completion) - cost,
    )


class HybridModelRouter:
    """Chooses Flash or Pro for each step and meters every call."""

    def __init__(
        self,
        *,
        flash: BaseChatModel,
        pro: BaseChatModel,
        flash_spec: ModelSpec,
        pro_spec: ModelSpec,
        tools: Sequence[BaseTool],
        max_fix_attempts: int,
        max_tool_calls: int,
        ledger: CostLedger | None = None,
    ) -> None:
        self.flash_spec = flash_spec
        self.pro_spec = pro_spec
        self.max_fix_attempts = max_fix_attempts
        self.max_tool_calls = max_tool_calls
        self.ledger = ledger or CostLedger()
        self._flash: Runnable[LanguageModelInput, BaseMessage] = flash.bind_tools(list(tools))
        self._pro: Runnable[LanguageModelInput, BaseMessage] = pro

    @classmethod
    def from_settings(cls, settings: Settings, tools: Sequence[BaseTool]) -> HybridModelRouter:
        api_key = SecretStr(settings.secret("deepseek_api_key"))
        flash_spec = ModelSpec(
            tier="flash",
            model_id=settings.deepseek_flash_model,
            label="DeepSeek V4 Flash",
            input_price_per_m=settings.flash_input_price_per_m,
            output_price_per_m=settings.flash_output_price_per_m,
            cached_input_price_per_m=settings.flash_cached_input_price_per_m,
        )
        pro_spec = ModelSpec(
            tier="pro",
            model_id=settings.deepseek_pro_model,
            label="DeepSeek V4 Pro",
            input_price_per_m=settings.pro_input_price_per_m,
            output_price_per_m=settings.pro_output_price_per_m,
            cached_input_price_per_m=settings.pro_cached_input_price_per_m,
        )
        flash = ChatOpenAI(
            model_name=flash_spec.model_id,
            openai_api_key=api_key,
            openai_api_base=settings.deepseek_base_url,
            temperature=0.0,
            request_timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )
        pro = ChatOpenAI(
            model_name=pro_spec.model_id,
            openai_api_key=api_key,
            openai_api_base=settings.deepseek_base_url,
            request_timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )
        return cls(
            flash=flash,
            pro=pro,
            flash_spec=flash_spec,
            pro_spec=pro_spec,
            tools=tools,
            max_fix_attempts=settings.sql_max_fix_attempts,
            max_tool_calls=settings.agent_max_tool_calls,
        )

    # --- Routing decisions ------------------------------------------------------------------

    def route_after_tools(self, state: AgentState) -> RouteAfterTools:
        """SQL failure -> Pro repair; tool budget spent -> Pro synthesis; else back to Flash."""
        if state["pending_sql_failure"] is not None:
            return "sql_fix"
        if state["tool_calls_made"] >= self.max_tool_calls:
            return "synthesize"
        return "agent"

    def ensure_fix_budget(self, state: AgentState) -> None:
        """Raise `SQLRetryExhaustedError` once Pro has used every repair attempt."""
        attempts = state["sql_fix_attempts"]
        if attempts >= self.max_fix_attempts:
            failure = state["pending_sql_failure"]
            raise SQLRetryExhaustedError(attempts, failure["error"] if failure else "unknown")

    def needs_synthesis(self, state: AgentState) -> bool:
        """Multi-source turns (2+ distinct tools succeeded) get a Pro-written final answer."""
        return len(set(state["tools_used"])) >= 2

    def remaining_tool_budget(self, state: AgentState) -> int:
        return max(self.max_tool_calls - state["tool_calls_made"], 0)

    # --- Metered calls ----------------------------------------------------------------------

    def call_flash(self, messages: Sequence[BaseMessage]) -> LLMCall:
        return self._call(self._flash, self.flash_spec, messages)

    def call_pro(self, messages: Sequence[BaseMessage]) -> LLMCall:
        return self._call(self._pro, self.pro_spec, messages)

    def _call(
        self,
        runnable: Runnable[LanguageModelInput, BaseMessage],
        spec: ModelSpec,
        messages: Sequence[BaseMessage],
    ) -> LLMCall:
        started = time.perf_counter()
        raw = runnable.invoke(list(messages))
        latency_ms = int((time.perf_counter() - started) * 1000)
        message = raw if isinstance(raw, AIMessage) else AIMessage(content=raw.content)
        usage = usage_from_message(message, spec, self.pro_spec)
        self.ledger.record(spec.tier, usage)
        return LLMCall(message=message, spec=spec, usage=usage, latency_ms=latency_ms)
