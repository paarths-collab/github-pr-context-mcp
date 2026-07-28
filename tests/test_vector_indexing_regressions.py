"""Offline vector-store regression tests using a minimal in-memory Chroma fake."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Collection:
    def __init__(self, metadata):
        self.metadata = dict(metadata or {})
        self.records = {}

    def count(self):
        return len(self.records)

    def upsert(self, *, documents, embeddings, metadatas, ids):
        for document, embedding, metadata, identifier in zip(documents, embeddings, metadatas, ids):
            self.records[identifier] = {
                "document": document,
                "embedding": embedding,
                "metadata": dict(metadata),
            }

    def get(self, where=None, include=None):
        rows = list(self.records.items())
        if where:
            rows = [
                row for row in rows
                if all(row[1]["metadata"].get(key) == value for key, value in where.items())
            ]
        return {
            "ids": [identifier for identifier, _ in rows],
            "metadatas": [row["metadata"] for _, row in rows],
        }

    def delete(self, *, ids=None, where=None):
        if ids is not None:
            for identifier in ids:
                self.records.pop(identifier, None)
            return
        for identifier, row in list(self.records.items()):
            if where and all(row["metadata"].get(key) == value for key, value in where.items()):
                self.records.pop(identifier)

    def modify(self, *, metadata):
        self.metadata = dict(metadata)


class _Client:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, *, name, metadata):
        # Chroma 0.5 replaces collection metadata when metadata is supplied
        # to get_or_create_collection for an existing collection.
        if name in self.collections:
            self.collections[name].metadata = dict(metadata or {})
        else:
            self.collections[name] = _Collection(metadata)
        return self.collections[name]

    def get_collection(self, name):
        return self.collections[name]

    def delete_collection(self, name):
        del self.collections[name]

    def list_collections(self):
        return list(self.collections.values())


class _CursorStore:
    def __init__(self):
        self.set_calls = []
        self.clear_calls = []
        self.checkpoint_calls = []
        self.complete_calls = []

    def set_cursor(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))

    def clear_cursor(self, *args, **kwargs):
        self.clear_calls.append((args, kwargs))

    def checkpoint_refresh(self, *args, **kwargs):
        self.checkpoint_calls.append((args, kwargs))

    def complete_refresh(self, *args, **kwargs):
        self.complete_calls.append((args, kwargs))


def _load_vector_store(monkeypatch):
    persistent = _Client()
    ephemeral = _Client()
    cursor_store = _CursorStore()
    fail_encoding = {"value": False}

    chromadb = types.ModuleType("chromadb")
    chromadb.PersistentClient = lambda path: persistent
    chromadb.EphemeralClient = lambda: ephemeral

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None

    storage_package = types.ModuleType("storage")
    storage_package.__path__ = []
    encoder = types.ModuleType("storage.encoder")
    encoder.encode = lambda text: [0.0]

    def encode_batch(texts):
        if fail_encoding["value"]:
            raise RuntimeError("embedding failed")
        return [[0.0] for _ in texts]

    encoder.encode_batch = encode_batch

    builder = types.ModuleType("storage.document_builder")

    def build_documents(prs):
        docs, metadatas, ids = [], [], []
        for pr in prs:
            for document in pr.get("documents", []):
                docs.append(document["text"])
                metadatas.append({"type": document["type"], "pr_number": pr["number"]})
                ids.append(document["id"])
        return docs, metadatas, ids

    builder.build_documents = build_documents
    cursor_module = types.ModuleType("storage.cursor_store")
    cursor_module.cursor_store = cursor_store
    storage_package.encoder = encoder
    storage_package.document_builder = builder
    storage_package.cursor_store = cursor_module

    monkeypatch.setitem(sys.modules, "chromadb", chromadb)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    monkeypatch.setitem(sys.modules, "storage", storage_package)
    monkeypatch.setitem(sys.modules, "storage.encoder", encoder)
    monkeypatch.setitem(sys.modules, "storage.document_builder", builder)
    monkeypatch.setitem(sys.modules, "storage.cursor_store", cursor_module)

    spec = importlib.util.spec_from_file_location(
        "vector_store_under_test", PROJECT_ROOT / "storage" / "vector_store.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, cursor_store, fail_encoding


def _pr(number, documents, *, truncated_connections=None):
    return {
        "number": number,
        "updated_at": "2024-05-03T12:00:00Z",
        "documents": documents,
        "truncated_connections": truncated_connections or [],
    }


def test_failed_embedding_does_not_advance_refresh_watermark(monkeypatch):
    vector_store, cursor_store, fail_encoding = _load_vector_store(monkeypatch)
    fail_encoding["value"] = True

    with pytest.raises(RuntimeError, match="embedding failed"):
        vector_store.index_prs(
            "acme/widget",
            [_pr(1, [{"id": "pr-1-desc", "type": "pr_description", "text": "body"}])],
        )

    assert cursor_store.set_calls == []


def test_stats_reads_do_not_reset_existing_chroma_metadata(monkeypatch):
    vector_store, _, _ = _load_vector_store(monkeypatch)
    repo = "acme/widget"

    vector_store.index_prs(
        repo,
        [_pr(1, [{"id": "pr-1-desc", "type": "pr_description", "text": "body"}])],
    )

    stats = vector_store.get_collection_stats(repo)

    assert stats["last_indexed_at"] is not None


def test_complete_refresh_removes_stale_documents_but_partial_nested_data_does_not(monkeypatch):
    vector_store, _, _ = _load_vector_store(monkeypatch)
    repo = "acme/widget"
    initial = _pr(
        1,
        [
            {"id": "pr-1-desc", "type": "pr_description", "text": "description"},
            {"id": "pr-1-comment-0", "type": "review_comment", "text": "comment"},
        ],
    )
    vector_store.index_prs(repo, [initial])
    collection = vector_store._get_collection(repo)
    assert collection.count() == 2

    vector_store.index_prs(repo, [_pr(1, [])])
    assert collection.count() == 0

    partial_repo = "acme/partial-widget"
    vector_store.index_prs(
        partial_repo,
        [_pr(2, [{"id": "pr-2-comment-0", "type": "review_comment", "text": "comment"}])],
    )
    partial_collection = vector_store._get_collection(partial_repo)
    vector_store.index_prs(
        partial_repo,
        [_pr(2, [], truncated_connections=["reviewThreads.comments"])],
    )
    assert partial_collection.count() == 1


def test_namespace_scope_and_deletion_cursor_scope_are_distinct(monkeypatch):
    vector_store, cursor_store, _ = _load_vector_store(monkeypatch)
    repo = "acme/widget"

    assert vector_store._collection_name(repo, None) != vector_store._collection_name(repo, "_default")
    vector_store._get_collection(repo, temporary=True, namespace="_default")
    vector_store.delete_repo_index(repo, storage="temporary", namespace="_default")

    assert cursor_store.clear_calls == [
        ((repo,), {"namespace": "_default", "storage": "temporary"})
    ]


def test_deleted_scope_rejects_a_late_index_write(monkeypatch):
    vector_store, cursor_store, _ = _load_vector_store(monkeypatch)
    repo = "acme/widget"
    epoch = vector_store.get_index_epoch(repo)

    vector_store.delete_repo_index(repo)

    with pytest.raises(vector_store.IndexInvalidatedError):
        vector_store.index_prs(
            repo,
            [_pr(1, [{"id": "pr-1-desc", "type": "pr_description", "text": "body"}])],
            expected_epoch=epoch,
        )

    assert vector_store._persistent_client.collections == {}
    assert cursor_store.set_calls == []


def test_deleted_scope_rejects_a_late_empty_check(monkeypatch):
    vector_store, _, _ = _load_vector_store(monkeypatch)
    repo = "acme/widget"
    epoch = vector_store.get_index_epoch(repo, temporary=True)

    vector_store.delete_repo_index(repo, storage="temporary")

    with pytest.raises(vector_store.IndexInvalidatedError):
        vector_store.mark_repo_checked(repo, temporary=True, expected_epoch=epoch)

    assert vector_store._ephemeral_client.collections == {}
