"""SOP ingestion: polymorphic chunking, SHA-256 content cache, Pinecone Serverless upsert.

Vector IDs are `<file>#<sha256(chunk)[:24]>`. Before embedding, the indexer lists the IDs
already stored under each file's prefix: chunks whose hash is present are skipped (no
embedding cost), new chunks are embedded and upserted, and IDs that no longer match any
chunk are deleted. Re-running on unchanged files therefore embeds nothing.
"""

from __future__ import annotations

import hashlib
import re
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from openai import OpenAI
from pinecone import Pinecone, ServerlessSpec
from pypdf import PdfReader

from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.exceptions import ConfigurationError
from cold_chain_copilot.core.logging import get_logger

logger = get_logger(__name__)

SUPPORTED_SUFFIXES = (".md", ".txt", ".pdf", ".csv", ".xlsx")
MAX_CHUNK_CHARS = 1400
CHUNK_OVERLAP_CHARS = 200
TABLE_ROWS_PER_CHUNK = 20
EMBED_BATCH_SIZE = 96
UPSERT_BATCH_SIZE = 100
_HASH_CHARS = 24
_METADATA_TEXT_LIMIT = 30_000

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class SopChunk:
    source_file: str
    section: str
    chunk_index: int
    text: str
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        digest = hashlib.sha256(f"{self.section}\n{self.text}".encode()).hexdigest()
        object.__setattr__(self, "sha256", digest)

    @property
    def vector_id(self) -> str:
        return f"{id_prefix(self.source_file)}{self.sha256[:_HASH_CHARS]}"

    def metadata(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "section": self.section,
            "chunk_index": self.chunk_index,
            "sha256": self.sha256,
            "text": self.text[:_METADATA_TEXT_LIMIT],
        }

    def embedding_input(self) -> str:
        return f"{self.source_file} | {self.section}\n{self.text}"


@dataclass(slots=True)
class IndexReport:
    files: int = 0
    chunks: int = 0
    embedded: int = 0
    skipped: int = 0
    deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "files": self.files,
            "chunks": self.chunks,
            "embedded": self.embedded,
            "skipped": self.skipped,
            "deleted": self.deleted,
        }


def id_prefix(source_file: str) -> str:
    """ASCII-safe vector ID prefix for a source file."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", source_file)
    return f"{safe}#"


# --- Chunking --------------------------------------------------------------------------------


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Pack paragraphs into windows of at most `max_chars`, with a short overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    windows: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = (
            [paragraph]
            if len(paragraph) <= max_chars
            else [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)]
        )
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                windows.append(current)
                overlap = current[-CHUNK_OVERLAP_CHARS:]
                boundary = overlap.find(" ")
                tail = overlap[boundary + 1 :] if boundary != -1 else overlap
                current = f"{tail}\n\n{piece}" if len(tail) + len(piece) + 2 <= max_chars else piece
            else:
                current = piece
    if current:
        windows.append(current)
    return windows


def chunk_markdown(source_file: str, text: str) -> list[SopChunk]:
    """Split on headings (the nearest heading becomes the section label), then by size."""
    sections: list[tuple[str, list[str]]] = []
    trail: list[str] = []
    buffer: list[str] = []
    label = "Preamble"

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if any(part.strip() for part in buffer):
                sections.append((label, buffer))
            level = len(match.group(1))
            trail = [*trail[: level - 1], match.group(2)]
            label = trail[-1]
            buffer = []
        else:
            buffer.append(line)
    if any(part.strip() for part in buffer):
        sections.append((label, buffer))

    chunks: list[SopChunk] = []
    for section_label, lines in sections:
        for window in split_text("\n".join(lines)):
            chunks.append(SopChunk(source_file, section_label, len(chunks), window))
    return chunks


def chunk_plain_text(source_file: str, text: str) -> list[SopChunk]:
    return [
        SopChunk(source_file, f"Part {index + 1}", index, window)
        for index, window in enumerate(split_text(text))
    ]


def chunk_pdf(source_file: str, path: Path) -> list[SopChunk]:
    reader = PdfReader(path)
    chunks: list[SopChunk] = []
    for page_number, page in enumerate(reader.pages, start=1):
        page_text = (page.extract_text() or "").strip()
        for window in split_text(page_text):
            chunks.append(SopChunk(source_file, f"Page {page_number}", len(chunks), window))
    return chunks


def _frame_to_chunks(source_file: str, frame: pd.DataFrame, label: str) -> list[SopChunk]:
    frame = frame.fillna("")
    columns = [str(column) for column in frame.columns]
    records = frame.astype(str).to_numpy().tolist()
    chunks: list[SopChunk] = []
    for start in range(0, len(records), TABLE_ROWS_PER_CHUNK):
        block = records[start : start + TABLE_ROWS_PER_CHUNK]
        lines = [
            " | ".join(f"{column}: {value}" for column, value in zip(columns, row, strict=True))
            for row in block
        ]
        section = f"{label} rows {start + 1}-{start + len(block)}"
        chunks.append(
            SopChunk(source_file, section, start // TABLE_ROWS_PER_CHUNK, "\n".join(lines))
        )
    return chunks


def chunk_csv(source_file: str, path: Path) -> list[SopChunk]:
    return _frame_to_chunks(source_file, pd.read_csv(path, dtype=str), "Table")


def chunk_xlsx(source_file: str, path: Path) -> list[SopChunk]:
    sheets = pd.read_excel(path, sheet_name=None, dtype=str, engine="openpyxl")
    chunks: list[SopChunk] = []
    for sheet_name, frame in sheets.items():
        for chunk in _frame_to_chunks(source_file, frame, f"Sheet {sheet_name}"):
            chunks.append(SopChunk(source_file, chunk.section, len(chunks), chunk.text))
    return chunks


def chunk_file(path: Path) -> list[SopChunk]:
    """Dispatch on file type. Unsupported suffixes raise `ConfigurationError`."""
    suffix = path.suffix.lower()
    name = path.name
    if suffix == ".md":
        return chunk_markdown(name, path.read_text(encoding="utf-8"))
    if suffix == ".txt":
        return chunk_plain_text(name, path.read_text(encoding="utf-8"))
    if suffix == ".pdf":
        return chunk_pdf(name, path)
    if suffix == ".csv":
        return chunk_csv(name, path)
    if suffix == ".xlsx":
        return chunk_xlsx(name, path)
    raise ConfigurationError(f"Unsupported SOP file type: {path}")


def discover_policy_files(policy_dir: Path) -> list[Path]:
    if not policy_dir.is_dir():
        raise ConfigurationError(f"Policy directory not found: {policy_dir}")
    return sorted(
        path
        for path in policy_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


# --- Pinecone --------------------------------------------------------------------------------


def _batched[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class SopIndexer:
    """Synchronizes the policy directory with a Pinecone Serverless index."""

    def __init__(
        self,
        *,
        pinecone_client: Pinecone,
        openai_client: OpenAI,
        index_name: str,
        namespace: str,
        embedding_model: str,
        dimension: int,
        cloud: str,
        region: str,
    ) -> None:
        self._pc = pinecone_client
        self._openai = openai_client
        self._index_name = index_name
        self._namespace = namespace
        self._embedding_model = embedding_model
        self._dimension = dimension
        self._cloud = cloud
        self._region = region

    @classmethod
    def from_settings(cls, settings: Settings) -> SopIndexer:
        return cls(
            pinecone_client=Pinecone(api_key=settings.secret("pinecone_api_key")),
            openai_client=OpenAI(api_key=settings.secret("openai_api_key")),
            index_name=settings.pinecone_index_name,
            namespace=settings.pinecone_namespace,
            embedding_model=settings.openai_embedding_model,
            dimension=settings.embedding_dimensions,
            cloud=settings.pinecone_cloud,
            region=settings.pinecone_region,
        )

    def ensure_index(self, timeout_s: float = 120.0) -> None:
        """Create the cosine serverless index if missing and wait until it is ready."""
        if not self._pc.has_index(self._index_name):
            logger.info("pinecone_index_creating", index=self._index_name)
            self._pc.create_index(
                name=self._index_name,
                dimension=self._dimension,
                metric="cosine",
                spec=ServerlessSpec(cloud=self._cloud, region=self._region),
            )
        deadline = time.monotonic() + timeout_s
        while True:
            description = self._pc.describe_index(self._index_name)
            if description.status.ready:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"Pinecone index {self._index_name} not ready in {timeout_s}s")
            time.sleep(2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            dimension = description.dimension
        if dimension != self._dimension:
            raise ConfigurationError(
                f"Index {self._index_name} has dimension {dimension}, "
                f"but {self._embedding_model} produces {self._dimension}."
            )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _batched(texts, EMBED_BATCH_SIZE):
            response = self._openai.embeddings.create(
                model=self._embedding_model, input=list(batch)
            )
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return vectors

    def _existing_ids(self, prefix: str) -> set[str]:
        index = self._pc.Index(self._index_name)
        ids: set[str] = set()
        for page in index.list(prefix=prefix, namespace=self._namespace):
            ids.update(item.id for item in page if item.id)
        return ids

    def sync_file(self, path: Path, report: IndexReport) -> None:
        chunks = chunk_file(path)
        prefix = id_prefix(path.name)
        existing = self._existing_ids(prefix)
        wanted = {chunk.vector_id: chunk for chunk in chunks}

        fresh = [chunk for vector_id, chunk in wanted.items() if vector_id not in existing]
        stale = sorted(existing - wanted.keys())
        index = self._pc.Index(self._index_name)

        if fresh:
            embeddings = self.embed([chunk.embedding_input() for chunk in fresh])
            vectors = [
                {"id": chunk.vector_id, "values": values, "metadata": chunk.metadata()}
                for chunk, values in zip(fresh, embeddings, strict=True)
            ]
            for batch in _batched(vectors, UPSERT_BATCH_SIZE):
                index.upsert(vectors=list(batch), namespace=self._namespace, show_progress=False)
        for id_batch in _batched(stale, UPSERT_BATCH_SIZE):
            index.delete(ids=list(id_batch), namespace=self._namespace)

        report.files += 1
        report.chunks += len(wanted)
        report.embedded += len(fresh)
        report.skipped += len(wanted) - len(fresh)
        report.deleted += len(stale)
        logger.info(
            "sop_file_synced",
            file=path.name,
            chunks=len(wanted),
            embedded=len(fresh),
            skipped=len(wanted) - len(fresh),
            deleted=len(stale),
        )

    def sync_directory(
        self,
        policy_dir: Path,
        on_file: Callable[[Path], None] | None = None,
    ) -> IndexReport:
        self.ensure_index()
        report = IndexReport()
        for path in discover_policy_files(policy_dir):
            if on_file is not None:
                on_file(path)
            self.sync_file(path, report)
        return report


def index_sops(settings: Settings) -> IndexReport:
    """Entry point for `fde-copilot ingest-sops`."""
    indexer = SopIndexer.from_settings(settings)
    report = indexer.sync_directory(settings.policy_dir)
    logger.info("sop_index_synced", **report.as_dict())
    return report
