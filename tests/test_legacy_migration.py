"""Offline coverage for explicit v2-to-v3 local index migration."""

from storage.legacy_migration import migrate_legacy_storage


class _Collection:
    def __init__(self, name, metadata):
        self.name = name
        self.metadata = dict(metadata or {})
        self.records = {}

    def count(self):
        return len(self.records)

    def modify(self, *, metadata):
        self.metadata = dict(metadata)

    def upsert(self, *, ids, documents, metadatas, embeddings):
        for identifier, document, metadata, embedding in zip(
            ids, documents, metadatas, embeddings
        ):
            self.records[identifier] = {
                "document": document,
                "metadata": dict(metadata),
                "embedding": embedding,
            }

    def get(self, *, include, limit, offset):
        rows = list(self.records.items())[offset : offset + limit]
        return {
            "ids": [identifier for identifier, _ in rows],
            "documents": [row["document"] for _, row in rows],
            "metadatas": [row["metadata"] for _, row in rows],
            "embeddings": [row["embedding"] for _, row in rows],
        }


class _Client:
    def __init__(self):
        self.collections = {}

    def list_collections(self):
        return list(self.collections.values())

    def get_collection(self, name):
        return self.collections[name]

    def get_or_create_collection(self, *, name, metadata):
        return self.collections.setdefault(name, _Collection(name, metadata))


class _CursorStore:
    def __init__(self):
        self.calls = []

    def migrate_legacy_cursors(self, *, dry_run):
        self.calls.append(dry_run)
        return {"migrated": 0, "skipped": 0, "dry_run": dry_run}


def _target_name(repo_key, namespace):
    return f"v3::{repo_key}::{namespace if namespace is not None else 'default'}"


def _target_metadata(repo_key, namespace):
    metadata = {"repo": repo_key, "hnsw:space": "cosine"}
    if namespace is not None:
        metadata["namespace"] = namespace
    return metadata


def _seed_legacy_collection(client):
    legacy = client.get_or_create_collection(
        name="acme--widget",
        metadata={"repo": "acme/widget", "hnsw:space": "cosine"},
    )
    legacy.upsert(
        ids=["default", "team-a", "literal-default"],
        documents=["default document", "team document", "literal default document"],
        metadatas=[{}, {"namespace": "team-a"}, {"namespace": "_default"}],
        embeddings=[[0.1], [0.2], [0.3]],
    )
    return legacy


def test_migration_copies_legacy_namespaces_without_reembedding_or_deleting_source():
    client = _Client()
    source = _seed_legacy_collection(client)
    cursor_store = _CursorStore()

    report = migrate_legacy_storage(
        client,
        cursor_store,
        collection_name_for_scope=_target_name,
        collection_metadata_for_scope=_target_metadata,
    )

    assert report["migrated_collections"] == 3
    assert report["migrated_documents"] == 3
    assert source.count() == 3
    assert client.collections[_target_name("acme/widget", None)].records["default"] == {
        "document": "default document",
        "metadata": {},
        "embedding": [0.1],
    }
    assert client.collections[_target_name("acme/widget", "team-a")].records["team-a"]["embedding"] == [0.2]
    assert client.collections[_target_name("acme/widget", "_default")].records["literal-default"]["metadata"]["namespace"] == "_default"
    assert cursor_store.calls == [False]

    repeated = migrate_legacy_storage(
        client,
        cursor_store,
        collection_name_for_scope=_target_name,
        collection_metadata_for_scope=_target_metadata,
    )
    assert repeated["migrated_documents"] == 0
    assert len(repeated["skipped"]) == 3


def test_dry_run_leaves_legacy_data_untouched_and_creates_no_v3_targets():
    client = _Client()
    _seed_legacy_collection(client)

    report = migrate_legacy_storage(
        client,
        _CursorStore(),
        dry_run=True,
        collection_name_for_scope=_target_name,
        collection_metadata_for_scope=_target_metadata,
    )

    assert report["migrated_collections"] == 3
    assert report["migrated_documents"] == 3
    assert set(client.collections) == {"acme--widget"}
