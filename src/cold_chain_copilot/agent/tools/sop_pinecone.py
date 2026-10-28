"""SOP semantic retrieval over Pinecone with `[SOP: file §section]` provenance."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, tool
from openai import OpenAI, OpenAIError
from pinecone import Pinecone
from pinecone.exceptions import PineconeException
from pydantic import BaseModel, Field

from cold_chain_copilot.agent.tools import SOP_TOOL_NAME, ToolOutcome
from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.logging import get_logger

logger = get_logger(__name__)

_EMBED_CACHE_SIZE = 256


@dataclass(frozen=True, slots=True)
class SopHit:
    vector_id: str
    source_file: str
    section: str
    score: float
    text: str

    @property
    def citation(self) -> str:
        return f"[SOP: {self.source_file} §{self.section}]"

    def as_payload(self) -> dict[str, Any]:
        return {
            "citation": self.citation,
            "source_file": self.source_file,
            "section": self.section,
            "score": round(self.score, 4),
            "text": self.text,
        }


class SopSearchInput(BaseModel):
    query: str = Field(
        min_length=3,
        max_length=500,
        description="Natural-language policy question, e.g. 'biologics excursion over 30 minutes'.",
    )
    top_k: int = Field(default=4, ge=1, le=8, description="Number of excerpts to return.")


class SopRetriever:
    """Embeds queries with OpenAI and searches the Pinecone SOP namespace."""

    def __init__(
        self,
        *,
        openai_client: OpenAI,
        pinecone_client: Pinecone,
        index_name: str,
        namespace: str,
        embedding_model: str,
    ) -> None:
        self._openai = openai_client
        self._pc = pinecone_client
        self._index_name = index_name
        self._namespace = namespace
        self._embedding_model = embedding_model
        self._index: Any = None
        self._lock = threading.Lock()
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()

    @classmethod
    def from_settings(cls, settings: Settings) -> SopRetriever:
        return cls(
            openai_client=OpenAI(
                api_key=settings.secret("openai_api_key"), timeout=20.0, max_retries=2
            ),
            pinecone_client=Pinecone(api_key=settings.secret("pinecone_api_key")),
            index_name=settings.pinecone_index_name,
            namespace=settings.pinecone_namespace,
            embedding_model=settings.openai_embedding_model,
        )

    def _get_index(self) -> Any:
        with self._lock:
            if self._index is None:
                self._index = self._pc.Index(self._index_name)
            return self._index

    def _embed(self, query: str) -> list[float]:
        key = query.strip().lower()
        with self._lock:
            cached = self._embed_cache.get(key)
            if cached is not None:
                self._embed_cache.move_to_end(key)
                return cached
        response = self._openai.embeddings.create(model=self._embedding_model, input=[query])
        vector = list(response.data[0].embedding)
        with self._lock:
            self._embed_cache[key] = vector
            if len(self._embed_cache) > _EMBED_CACHE_SIZE:
                self._embed_cache.popitem(last=False)
        return vector

    def search(self, query: str, top_k: int) -> list[SopHit]:
        response = self._get_index().query(
            vector=self._embed(query),
            top_k=top_k,
            namespace=self._namespace,
            include_metadata=True,
        )
        hits: list[SopHit] = []
        for match in response.matches:
            metadata = dict(match.metadata or {})
            text = str(metadata.get("text") or "").strip()
            if not text:
                continue
            hits.append(
                SopHit(
                    vector_id=str(match.id),
                    source_file=str(metadata.get("source_file") or "unknown"),
                    section=str(metadata.get("section") or "General"),
                    score=float(match.score or 0.0),
                    text=text,
                )
            )
        return hits


SopSearcher = Callable[[str, int], list[SopHit]]


def run_sop_tool(query: str, top_k: int, *, search: SopSearcher) -> ToolOutcome:
    try:
        hits = search(query, top_k)
    except (OpenAIError, PineconeException) as exc:
        logger.warning("sop_search_failed", error=str(exc))
        return ToolOutcome(
            tool_name=SOP_TOOL_NAME,
            status="error",
            content=f"UPSTREAM_ERROR: SOP search is unavailable ({exc.__class__.__name__}).",
            error_code="UPSTREAM_ERROR",
            error=str(exc),
        )
    if not hits:
        return ToolOutcome(
            tool_name=SOP_TOOL_NAME,
            status="ok",
            content="No SOP excerpts matched this query. Do not cite the SOP for it.",
            payload={"hits": []},
        )
    content = "\n\n".join(f"{hit.citation} (relevance {hit.score:.2f})\n{hit.text}" for hit in hits)
    return ToolOutcome(
        tool_name=SOP_TOOL_NAME,
        status="ok",
        content=content,
        payload={"hits": [hit.as_payload() for hit in hits]},
    )


def build_sop_tool(search: SopSearcher) -> BaseTool:
    """The LangChain tool bound to the Flash model."""

    @tool(SOP_TOOL_NAME, args_schema=SopSearchInput)
    def search_sop(query: str, top_k: int = 4) -> ToolOutcome:
        """Search the cold chain SOP library for policy, thresholds, time limits, severity
        levels, escalation contacts, and required actions. Each excerpt starts with the
        citation tag to use, e.g. [SOP: cold_chain_sop_v4.md §6. Weather Risk Management]."""
        return run_sop_tool(query, top_k, search=search)

    return search_sop
