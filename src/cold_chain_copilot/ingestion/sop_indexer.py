"""SOP ingestion: polymorphic chunking, SHA-256 caching, OpenAI embeddings, Pinecone upsert.

Two cache layers keep re-runs cheap and the index exact:

1. **Local manifest** (`data/policy/.ingest_cache.json`): the SHA-256 of every indexed file
   plus the vector IDs it produced. A file whose bytes are unchanged is skipped with no
   OpenAI or Pinecone calls at all.
2. **Content-addressed vector IDs** (`<file>#<sha256(chunk)[:24]>`): for a changed file,
   the IDs already in Pinecone are listed under the file's prefix. Only chunks that are not
   there yet are embedded, and IDs that no longer match a chunk are deleted. Pinecone stays
   the source of truth, so a lost or stale manifest never causes duplicate embeddings.

Files that disappear from the policy directory have their vectors deleted and their manifest
entry dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openai import OpenAI
from pinecone import Pinecone, ServerlessSpec
from pydantic import BaseModel, Field, ValidationError
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

CACHE_FILENAME = ".ingest_cache.json"
MANIFEST_VERSION = 1
# Bump whenever chunking or embedding-input formatting changes: it invalidates every
# manifest entry so files are re-diffed against Pinecone.
CHUNKER_VERSION = 1


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
    files_unchanged: int = 0
    files_removed: int = 0
    chunks: int = 0
    embedded: int = 0
    skipped: int = 0
    deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "files": self.files,
            "files_unchanged": self.files_unchanged,
            "files_removed": self.files_removed,
            "chunks": self.chunks,
            "embedded": self.embedded,
            "skipped": self.skipped,
            "deleted": self.deleted,
        }


# --- Local manifest --------------------------------------------------------------------------


class ManifestEntry(BaseModel):
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_ids: list[str]
    indexed_at_utc: datetime


class IngestManifest(BaseModel):
    """What was last indexed, and against which index/model, keyed by file name."""

    version: int = MANIFEST_VERSION
    chunker_version: int = CHUNKER_VERSION
    index_name: str
    namespace: str
    embedding_model: str
    dimension: int
    files: dict[str, ManifestEntry] = Field(default_factory=dict)

    def matches(self, other: IngestManifest) -> bool:
        """Same target index, namespace, model, and chunker: entries are reusable."""
        return (
            self.version,
            self.chunker_version,
            self.index_name,
            self.namespace,
            self.embedding_model,
            self.dimension,
        ) == (
            other.version,
            other.chunker_version,
            other.index_name,
            other.namespace,
            other.embedding_model,
            other.dimension,
        )

    @classmethod
    def load(cls, path: Path, expected: IngestManifest) -> IngestManifest:
        """Read the manifest; anything missing, corrupt, or for another target starts empty."""
        if not path.is_file():
            return expected
        try:
            stored = cls.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            logger.warning("sop_manifest_unreadable", path=str(path), error=str(exc))
            return expected
        if not stored.matches(expected):
            logger.info("sop_manifest_invalidated", path=str(path), reason="target changed")
            return expected
        return stored

    def save(self, path: Path) -> None:
        """Atomic write (temp file + rename) so an interrupted run never corrupts it."""
        payload = json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".ingest_cache.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            Path(tmp_name).replace(path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


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
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in SUPPORTED_SUFFIXES
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

    def _expected_manifest(self) -> IngestManifest:
        return IngestManifest(
            index_name=self._index_name,
            namespace=self._namespace,
            embedding_model=self._embedding_model,
            dimension=self._dimension,
        )

    def _delete_ids(self, ids: Sequence[str]) -> None:
        index = self._pc.Index(self._index_name)
        for id_batch in _batched(ids, UPSERT_BATCH_SIZE):
            index.delete(ids=list(id_batch), namespace=self._namespace)

    def sync_file(self, path: Path, report: IndexReport) -> list[str]:
        """Diff one file's chunks against Pinecone; return the file's current vector IDs."""
        chunks = chunk_file(path)
        existing = self._existing_ids(id_prefix(path.name))
        wanted = {chunk.vector_id: chunk for chunk in chunks}

        fresh = [chunk for vector_id, chunk in wanted.items() if vector_id not in existing]
        stale = sorted(existing - wanted.keys())

        if fresh:
            index = self._pc.Index(self._index_name)
            embeddings = self.embed([chunk.embedding_input() for chunk in fresh])
            vectors = [
                {"id": chunk.vector_id, "values": values, "metadata": chunk.metadata()}
                for chunk, values in zip(fresh, embeddings, strict=True)
            ]
            for batch in _batched(vectors, UPSERT_BATCH_SIZE):
                index.upsert(vectors=list(batch), namespace=self._namespace, show_progress=False)
        self._delete_ids(stale)

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
        return sorted(wanted)

    def sync_directory(
        self,
        policy_dir: Path,
        *,
        use_cache: bool = True,
        on_file: Callable[[Path], None] | None = None,
    ) -> IndexReport:
        """Bring the index in line with `policy_dir`.

        With `use_cache=False` every file is re-diffed against Pinecone (still embedding only
        chunks that are missing), which repairs an index that was edited out of band.
        """
        self.ensure_index()
        manifest_path = policy_dir / CACHE_FILENAME
        manifest = IngestManifest.load(manifest_path, self._expected_manifest())
        report = IndexReport()
        present = discover_policy_files(policy_dir)

        for path in present:
            digest = file_sha256(path)
            entry = manifest.files.get(path.name)
            if use_cache and entry is not None and entry.file_sha256 == digest:
                report.files += 1
                report.files_unchanged += 1
                report.chunks += len(entry.chunk_ids)
                report.skipped += len(entry.chunk_ids)
                logger.info("sop_file_unchanged", file=path.name, chunks=len(entry.chunk_ids))
                continue
            if on_file is not None:
                on_file(path)
            chunk_ids = self.sync_file(path, report)
            manifest.files[path.name] = ManifestEntry(
                file_sha256=digest, chunk_ids=chunk_ids, indexed_at_utc=datetime.now(UTC)
            )
            manifest.save(manifest_path)

        present_names = {path.name for path in present}
        for removed in sorted(set(manifest.files) - present_names):
            ids = sorted(
                self._existing_ids(id_prefix(removed)) | set(manifest.files[removed].chunk_ids)
            )
            self._delete_ids(ids)
            del manifest.files[removed]
            report.files_removed += 1
            report.deleted += len(ids)
            logger.info("sop_file_removed", file=removed, deleted=len(ids))

        manifest.save(manifest_path)
        logger.info("sop_index_synced", **report.as_dict())
        return report
