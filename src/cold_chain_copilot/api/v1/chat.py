"""POST /api/v1/chat — run one LangGraph turn and return the answer with its trace."""

from __future__ import annotations

import openai
from fastapi import APIRouter, HTTPException, status
from langgraph.errors import GraphRecursionError

from cold_chain_copilot.api.dependencies import CopilotDep
from cold_chain_copilot.api.schemas import ChatRequest, ChatResponse
from cold_chain_copilot.core.logging import get_logger

router = APIRouter(prefix="/chat", tags=["copilot"])
logger = get_logger(__name__)


@router.post("", response_model=ChatResponse, summary="Run one dispatcher turn")
def chat(body: ChatRequest, copilot: CopilotDep) -> ChatResponse:
    """Synchronous on purpose: FastAPI runs it in the threadpool, keeping the event loop free
    while the graph waits on DeepSeek, PostgreSQL, Open-Meteo, and Pinecone."""
    try:
        result = copilot.run_turn(body.message, session_id=body.session_id)
    except openai.APIStatusError as exc:
        logger.warning("model_provider_error", status_code=exc.status_code, error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Model provider returned HTTP {exc.status_code}. Please retry shortly.",
        ) from exc
    except openai.OpenAIError as exc:
        logger.warning("model_provider_unreachable", error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Model provider is unreachable. Please retry shortly."
        ) from exc
    except GraphRecursionError as exc:
        logger.warning("turn_step_budget_exceeded")
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This question needed more agent steps than allowed. Try narrowing it.",
        ) from exc
    return ChatResponse.model_validate(result.model_dump())
