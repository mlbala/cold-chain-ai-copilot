"""POST /api/v1/chat — run one LangGraph turn and return the answer with its trace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import openai
from fastapi import APIRouter, HTTPException, Request, status
from langgraph.errors import GraphRecursionError

from cold_chain_copilot.api.dependencies import CopilotDep, GuardsDep, client_id
from cold_chain_copilot.api.schemas import ChatRequest, ChatResponse
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.core.metrics import metrics

router = APIRouter(prefix="/chat", tags=["copilot"])
logger = get_logger(__name__)


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds()) + 1


@router.post("", response_model=ChatResponse, summary="Run one dispatcher turn")
def chat(
    body: ChatRequest, request: Request, copilot: CopilotDep, guards: GuardsDep
) -> ChatResponse:
    """Synchronous on purpose: FastAPI runs it in the threadpool, keeping the event loop free
    while the graph waits on DeepSeek, PostgreSQL, Open-Meteo, and Pinecone.

    Guardrails run before any model call, cheapest first: per-client rate limit, daily
    model budget, then the concurrency cap that protects the 1 GB host.
    """
    retry_after = guards.limiter.try_acquire(client_id(request))
    if retry_after is not None:
        metrics.increment("chat_rate_limited")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Rate limit: at most {guards.limiter.max_events} copilot turns per minute.",
            headers={"Retry-After": str(retry_after)},
        )
    if guards.daily_budget_usd and copilot.router.ledger.spent_today() >= guards.daily_budget_usd:
        metrics.increment("llm_budget_blocked")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Daily model budget of ${guards.daily_budget_usd:.2f} reached; resets 00:00 UTC.",
            headers={"Retry-After": str(_seconds_until_utc_midnight())},
        )
    if not guards.slots.acquire(blocking=False):
        metrics.increment("chat_busy_rejected")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"The copilot is handling {guards.max_concurrent} turns; retry in a few seconds.",
            headers={"Retry-After": "5"},
        )
    try:
        result = copilot.run_turn(body.message, session_id=body.session_id)
    except openai.APIStatusError as exc:
        metrics.increment("chat_errors")
        logger.warning("model_provider_error", status_code=exc.status_code, error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Model provider returned HTTP {exc.status_code}. Please retry shortly.",
        ) from exc
    except openai.OpenAIError as exc:
        metrics.increment("chat_errors")
        logger.warning("model_provider_unreachable", error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Model provider is unreachable. Please retry shortly."
        ) from exc
    except GraphRecursionError as exc:
        metrics.increment("chat_errors")
        logger.warning("turn_step_budget_exceeded")
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This question needed more agent steps than allowed. Try narrowing it.",
        ) from exc
    finally:
        guards.slots.release()
    metrics.increment("chat_turns")
    return ChatResponse.model_validate(result.model_dump())
