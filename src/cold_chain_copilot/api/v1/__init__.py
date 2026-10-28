"""Version 1 of the HTTP API, mounted at `/api/v1`."""

from __future__ import annotations

from fastapi import APIRouter

from cold_chain_copilot.api.v1 import audit, chat, fleet

api_router = APIRouter()
api_router.include_router(chat.router)
api_router.include_router(fleet.router)
api_router.include_router(audit.router)

__all__ = ["api_router"]
