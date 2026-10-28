"""SOP indexer: polymorphic chunking and the local SHA-256 manifest cache."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openpyxl import Workbook

from cold_chain_copilot.ingestion.sop_indexer import (
    CACHE_FILENAME,
    SopIndexer,
    chunk_file,
    discover_policy_files,
    id_prefix,
)

DIM = 8

SOP_TEXT = """# Test SOP

## 1. Doors

1.1 Doors may be opened only at an assigned dock.

## 2. Batteries

2.1 Below 25 % is a warning. Below 15 % is critical.
"""


class FakeIndex:
    def __init__(self) -> None:
        self.vectors: dict[str, dict[str, Any]] = {}
        self.list_calls = 0
        self.upserted: list[str] = []
        self.deleted: list[str] = []

    def list(self, *, prefix: str, namespace: str) -> Iterator[list[SimpleNamespace]]:
        self.list_calls += 1
        ids = sorted(i for i in self.vectors if i.startswith(prefix))
        if ids:
            yield [SimpleNamespace(id=i) for i in ids]

    def upsert(self, *, vectors: list[dict[str, Any]], namespace: str, show_progress: bool) -> None:
        for vector in vectors:
            self.vectors[vector["id"]] = vector
            self.upserted.append(vector["id"])

    def delete(self, *, ids: list[str], namespace: str) -> None:
        for vector_id in ids:
            self.vectors.pop(vector_id, None)
            self.deleted.append(vector_id)


class FakePinecone:
    def __init__(self) -> None:
        self.index = FakeIndex()

    def has_index(self, name: str) -> bool:
        return True

    def describe_index(self, name: str) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(ready=True), dimension=DIM)

    def Index(self, name: str) -> FakeIndex:  # noqa: N802 - mirrors the Pinecone client API
        return self.index


class FakeEmbeddings:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def create(self, *, model: str, input: list[str]) -> SimpleNamespace:
        self.inputs.extend(input)
        return SimpleNamespace(
            data=[SimpleNamespace(index=i, embedding=[0.1] * DIM) for i in range(len(input))]
        )


class FakeOpenAI:
    def __init__(self) -> None:
        self.embeddings = FakeEmbeddings()


@pytest.fixture
def policy_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "policy"
    directory.mkdir()
    (directory / "sop.md").write_text(SOP_TEXT, encoding="utf-8")
    return directory


def make_indexer(pc: FakePinecone, oa: FakeOpenAI, model: str = "text-embedding-3-small") -> Any:
    return SopIndexer(
        pinecone_client=pc,  # type: ignore[arg-type]
        openai_client=oa,  # type: ignore[arg-type]
        index_name="fde-sop-index-openai",
        namespace="sops",
        embedding_model=model,
        dimension=DIM,
        cloud="aws",
        region="us-east-1",
    )


def test_first_run_embeds_everything_and_writes_manifest(policy_dir: Path) -> None:
    pc, oa = FakePinecone(), FakeOpenAI()
    report = make_indexer(pc, oa).sync_directory(policy_dir)

    assert report.embedded == report.chunks > 0
    assert report.files_unchanged == 0
    manifest = json.loads((policy_dir / CACHE_FILENAME).read_text())
    entry = manifest["files"]["sop.md"]
    assert len(entry["file_sha256"]) == 64
    assert sorted(entry["chunk_ids"]) == sorted(pc.index.vectors)
    assert manifest["index_name"] == "fde-sop-index-openai"


def test_unchanged_file_is_skipped_without_any_api_call(policy_dir: Path) -> None:
    pc, oa = FakePinecone(), FakeOpenAI()
    make_indexer(pc, oa).sync_directory(policy_dir)
    list_calls, embedded = pc.index.list_calls, len(oa.embeddings.inputs)

    report = make_indexer(pc, oa).sync_directory(policy_dir)

    assert report.files_unchanged == 1
    assert report.embedded == 0
    assert report.skipped == report.chunks > 0
    assert pc.index.list_calls == list_calls
    assert len(oa.embeddings.inputs) == embedded


def test_edited_file_embeds_only_new_chunks_and_deletes_stale(policy_dir: Path) -> None:
    pc, oa = FakePinecone(), FakeOpenAI()
    make_indexer(pc, oa).sync_directory(policy_dir)
    before = set(pc.index.vectors)

    edited = SOP_TEXT.replace("Below 15 % is critical.", "Below 10 % is critical.")
    (policy_dir / "sop.md").write_text(edited + "\n## 3. Weather\n\n3.1 Gusts over 65 km/h.\n")
    report = make_indexer(pc, oa).sync_directory(policy_dir)

    assert report.embedded == 2
    assert report.deleted == 1
    assert report.skipped == report.chunks - 2
    assert len(before & set(pc.index.vectors)) == report.skipped


def test_removed_file_vectors_are_deleted(policy_dir: Path) -> None:
    pc, oa = FakePinecone(), FakeOpenAI()
    (policy_dir / "extra.txt").write_text("Extra policy paragraph about seals.\n")
    make_indexer(pc, oa).sync_directory(policy_dir)
    assert any(i.startswith(id_prefix("extra.txt")) for i in pc.index.vectors)

    (policy_dir / "extra.txt").unlink()
    report = make_indexer(pc, oa).sync_directory(policy_dir)

    assert report.files_removed == 1
    assert not any(i.startswith(id_prefix("extra.txt")) for i in pc.index.vectors)
    assert "extra.txt" not in json.loads((policy_dir / CACHE_FILENAME).read_text())["files"]


def test_corrupt_manifest_falls_back_to_pinecone_without_reembedding(policy_dir: Path) -> None:
    pc, oa = FakePinecone(), FakeOpenAI()
    make_indexer(pc, oa).sync_directory(policy_dir)
    (policy_dir / CACHE_FILENAME).write_text("{not json")
    embedded = len(oa.embeddings.inputs)

    report = make_indexer(pc, oa).sync_directory(policy_dir)

    assert report.embedded == 0
    assert report.files_unchanged == 0
    assert len(oa.embeddings.inputs) == embedded
    json.loads((policy_dir / CACHE_FILENAME).read_text())


def test_model_change_invalidates_manifest(policy_dir: Path) -> None:
    pc, oa = FakePinecone(), FakeOpenAI()
    make_indexer(pc, oa).sync_directory(policy_dir)
    list_calls = pc.index.list_calls

    report = make_indexer(pc, oa, model="text-embedding-3-large").sync_directory(policy_dir)

    assert report.files_unchanged == 0
    assert pc.index.list_calls > list_calls


def test_ignore_cache_rediffs_against_pinecone(policy_dir: Path) -> None:
    pc, oa = FakePinecone(), FakeOpenAI()
    make_indexer(pc, oa).sync_directory(policy_dir)
    pc.index.vectors.pop(sorted(pc.index.vectors)[0])

    cached = make_indexer(pc, oa).sync_directory(policy_dir)
    repaired = make_indexer(pc, oa).sync_directory(policy_dir, use_cache=False)

    assert cached.embedded == 0
    assert repaired.embedded == 1


def test_polymorphic_chunking(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("First paragraph.\n\nSecond paragraph.\n")
    (tmp_path / "limits.csv").write_text("cargo,min_c,max_c\nVaccines,2,8\nIce Cream,-28,-20\n")
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Contacts"
    sheet.append(["role", "notify_within_min"])
    sheet.append(["QA Duty Officer", 10])
    workbook.save(tmp_path / "matrix.xlsx")
    (tmp_path / "sop.md").write_text(SOP_TEXT)
    (tmp_path / CACHE_FILENAME).write_text("{}")
    (tmp_path / "._sop.md").write_text("resource fork")

    files = discover_policy_files(tmp_path)
    assert [p.name for p in files] == ["limits.csv", "matrix.xlsx", "notes.txt", "sop.md"]

    md = chunk_file(tmp_path / "sop.md")
    assert [c.section for c in md] == ["1. Doors", "2. Batteries"]
    csv_chunk = chunk_file(tmp_path / "limits.csv")[0]
    assert "cargo: Vaccines | min_c: 2 | max_c: 8" in csv_chunk.text
    xlsx_chunk = chunk_file(tmp_path / "matrix.xlsx")[0]
    assert xlsx_chunk.section.startswith("Sheet Contacts")
    assert "role: QA Duty Officer" in xlsx_chunk.text
    assert chunk_file(tmp_path / "notes.txt")[0].text.startswith("First paragraph.")
    assert len({c.vector_id for c in md}) == len(md)
