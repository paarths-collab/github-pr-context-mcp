"""Explicit, idempotent migration for pre-v3 local Chroma indexes.

v2 stored every namespace for a repository in one ``owner--repo`` collection.
v3 isolates each namespace in a hashed collection. This module copies (never
deletes) the old local data only when a user explicitly runs the migration CLI.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable


_BATCH_SIZE = 250


def _normalize_namespace(namespace: object) -> str | None:
    if not isinstance(namespace, str):
        return None
    normalized = namespace.strip()
    return normalized or None


def _legacy_collection_name(repo_key: str) -> str:
    return repo_key.replace("/", "--")


def _read_records(collection) -> list[dict[str, Any]]:
    """Read a legacy collection in bounded batches using Chroma's public API."""
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = collection.get(
            include=["documents", "metadatas", "embeddings"],
            limit=_BATCH_SIZE,
            offset=offset,
        )
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        embeddings = result.get("embeddings") or []
        if not ids:
            break
        if not (len(ids) == len(documents) == len(metadatas) == len(embeddings)):
            raise ValueError("Legacy collection returned misaligned document data.")
        records.extend(
            {
                "id": identifier,
                "document": document,
                "metadata": metadata if isinstance(metadata, dict) else {},
                "embedding": embedding,
            }
            for identifier, document, metadata, embedding in zip(
                ids, documents, metadatas, embeddings
            )
        )
        offset += len(ids)
        if len(ids) < _BATCH_SIZE:
            break
    return records


def _target_metadata(
    existing: dict[str, Any] | None,
    base: dict[str, Any],
    source_name: str,
    state: str,
) -> dict[str, Any]:
    metadata = dict(existing or base)
    metadata.update(
        {
            "schema_version": "v3",
            "migration_source": source_name,
            "migration_state": state,
        }
    )
    # Chroma's HNSW metadata is immutable after creation.
    return {key: value for key, value in metadata.items() if key != "hnsw:space"}


def migrate_legacy_storage(
    persistent_client,
    cursor_store,
    *,
    dry_run: bool = False,
    collection_name_for_scope: Callable[[str, str | None], str] | None = None,
    collection_metadata_for_scope: Callable[[str, str | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy v2 repository collections into v3 namespace-scoped collections.

    The caller supplies a Chroma persistent client. Tests may supply an in-memory
    fake, while the local CLI uses the real v3 client and naming functions.
    Existing ordinary v3 collections are never overwritten; legacy collections
    remain intact as a rollback backup.
    """
    if collection_name_for_scope is None or collection_metadata_for_scope is None:
        from storage.vector_store import _collection_metadata, _collection_name

        collection_name_for_scope = collection_name_for_scope or _collection_name
        collection_metadata_for_scope = (
            collection_metadata_for_scope or _collection_metadata
        )

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "migrated_collections": 0,
        "migrated_documents": 0,
        "skipped": [],
        "conflicts": [],
    }

    for listed_collection in persistent_client.list_collections():
        source_name = getattr(listed_collection, "name", None)
        source_metadata = getattr(listed_collection, "metadata", None) or {}
        repo_key = source_metadata.get("repo") if isinstance(source_metadata, dict) else None
        if not isinstance(source_name, str) or not isinstance(repo_key, str):
            continue
        if _legacy_collection_name(repo_key) != source_name:
            continue

        source = persistent_client.get_collection(source_name)
        try:
            records = _read_records(source)
        except ValueError as exc:
            report["skipped"].append({"source": source_name, "reason": str(exc)})
            continue

        records_by_namespace: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            namespace = _normalize_namespace(record["metadata"].get("namespace"))
            metadata = dict(record["metadata"])
            if namespace is None:
                metadata.pop("namespace", None)
            else:
                metadata["namespace"] = namespace
            record["metadata"] = metadata
            records_by_namespace[namespace].append(record)

        for namespace, scoped_records in records_by_namespace.items():
            target_name = collection_name_for_scope(repo_key, namespace)
            try:
                target = persistent_client.get_collection(target_name)
            except Exception:
                target = None

            target_metadata = getattr(target, "metadata", None) or {}
            target_count = target.count() if target is not None else 0
            source_matches = target_metadata.get("migration_source") == source_name
            migration_state = target_metadata.get("migration_state")
            if target is not None and source_matches and migration_state == "complete":
                report["skipped"].append(
                    {"source": source_name, "target": target_name, "reason": "already_migrated"}
                )
                continue
            if target is not None and target_count and not (
                source_matches and migration_state == "in_progress"
            ):
                report["conflicts"].append(
                    {
                        "source": source_name,
                        "target": target_name,
                        "reason": "nonempty_v3_target",
                    }
                )
                continue

            if dry_run:
                report["migrated_collections"] += 1
                report["migrated_documents"] += len(scoped_records)
                continue

            base_metadata = collection_metadata_for_scope(repo_key, namespace)
            target = target or persistent_client.get_or_create_collection(
                name=target_name,
                metadata=base_metadata,
            )
            target.modify(
                metadata=_target_metadata(
                    getattr(target, "metadata", None),
                    base_metadata,
                    source_name,
                    "in_progress",
                )
            )
            for start in range(0, len(scoped_records), _BATCH_SIZE):
                batch = scoped_records[start : start + _BATCH_SIZE]
                target.upsert(
                    ids=[record["id"] for record in batch],
                    documents=[record["document"] for record in batch],
                    metadatas=[record["metadata"] for record in batch],
                    embeddings=[record["embedding"] for record in batch],
                )
            target.modify(
                metadata=_target_metadata(
                    getattr(target, "metadata", None),
                    base_metadata,
                    source_name,
                    "complete",
                )
            )
            report["migrated_collections"] += 1
            report["migrated_documents"] += len(scoped_records)

    if hasattr(cursor_store, "migrate_legacy_cursors"):
        report["cursor_migration"] = cursor_store.migrate_legacy_cursors(
            dry_run=dry_run
        )
    return report
