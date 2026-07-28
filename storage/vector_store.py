# ChromaDB client management, indexing, querying, and repo listing.
# No ML model loading, no PR transformation, no GitHub calls here.

import chromadb
import hashlib
import os
import re
import sys
import threading
import datetime
from dotenv import load_dotenv
from storage.encoder import encode, encode_batch
from storage.document_builder import build_documents

load_dotenv()

_DEFAULT_CHROMA_DIR = os.path.join(os.path.expanduser("~"), ".github-pr-mcp", "chroma_db")
PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", _DEFAULT_CHROMA_DIR)

# Persistent = survives restarts, stored on disk
_persistent_client = chromadb.PersistentClient(path=PERSIST_DIR)

# Ephemeral = in-memory only, wiped when the MCP server process stops
_ephemeral_client = chromadb.EphemeralClient()

_chroma_lock = threading.Lock()
_INDEX_EPOCHS: dict[str, int] = {}


class IndexInvalidatedError(RuntimeError):
    """Raised when a deleted index job attempts a stale write."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize_namespace(namespace: str | None) -> str | None:
    if namespace is None:
        return None
    ns = namespace.strip()
    return ns or None


def _index_scope_key(repo_key: str, temporary: bool, namespace: str | None) -> str:
    ns = _normalize_namespace(namespace)
    scope = "default:" if ns is None else f"namespace:{ns}"
    storage = "temporary" if temporary else "permanent"
    return f"{repo_key}\0{scope}\0{storage}"


def get_index_epoch(
    repo_key: str,
    temporary: bool = False,
    namespace: str | None = None,
) -> int:
    """Capture the current in-process write epoch for an index scope."""
    with _chroma_lock:
        return _INDEX_EPOCHS.get(_index_scope_key(repo_key, temporary, namespace), 0)


def _assert_current_epoch(
    repo_key: str,
    temporary: bool,
    namespace: str | None,
    expected_epoch: int | None,
) -> None:
    if expected_epoch is None:
        return
    current_epoch = _INDEX_EPOCHS.get(_index_scope_key(repo_key, temporary, namespace), 0)
    if current_epoch != expected_epoch:
        raise IndexInvalidatedError(
            f"Index job for {repo_key} was invalidated by a delete request."
        )

def _safe_name(repo_key: str) -> str:
    safe = re.sub(r"[^a-z0-9._-]+", "-", repo_key.lower()).strip(".-")
    if not safe:
        return "repo"
    return safe if safe[0].isalnum() else f"repo-{safe}"

def _collection_name(repo_key: str, namespace: str | None = None) -> str:
    """Return a stable Chroma name scoped to both repository and namespace.

    A separate collection prevents one namespace from seeing, overwriting, or
    deleting another namespace's documents for the same GitHub repository.
    The digest avoids leaking the raw namespace in a collection name and keeps
    the result inside Chroma's 63-character collection-name limit.
    """
    ns = _normalize_namespace(namespace)
    scope = "default:" if ns is None else f"namespace:{ns}"
    digest = hashlib.sha256(f"{repo_key}\0{scope}".encode("utf-8")).hexdigest()[:12]
    return f"{_safe_name(repo_key)[:48]}--{digest}"

def _collection_metadata(repo_key: str, namespace: str | None = None) -> dict:
    metadata = {
        "hnsw:space": "cosine",
        "repo": repo_key,
    }
    ns = _normalize_namespace(namespace)
    if ns is not None:
        metadata["namespace"] = ns
    return metadata

def _collection_repo(col) -> str:
    meta = col.metadata or {}
    if "repo" in meta:
        return meta["repo"]
    return col.name.replace("--", "/")

def _collection_namespace(col) -> str | None:
    meta = col.metadata or {}
    ns = meta.get("namespace")
    return _normalize_namespace(ns) if isinstance(ns, str) else None

def _client(temporary: bool):
    return _ephemeral_client if temporary else _persistent_client

def _get_collection(repo_key: str, temporary: bool = False, namespace: str | None = None):
    return _client(temporary).get_or_create_collection(
        name=_collection_name(repo_key, namespace=namespace),
        metadata=_collection_metadata(repo_key, namespace=namespace),
    )


# ── Status checks ─────────────────────────────────────────────────────────────

def repo_is_indexed_permanently(repo_key: str, namespace: str | None = None) -> bool:
    try:
        col = _persistent_client.get_collection(_collection_name(repo_key, namespace=namespace))
        return col.count() > 0
    except Exception:
        return False

def repo_is_indexed_temporarily(repo_key: str, namespace: str | None = None) -> bool:
    try:
        col = _ephemeral_client.get_collection(_collection_name(repo_key, namespace=namespace))
        return col.count() > 0
    except Exception:
        return False


# ── Listing ───────────────────────────────────────────────────────────────────

def list_all_repos(namespace: str | None = None) -> list[dict]:
    ns_filter = _normalize_namespace(namespace)

    def _rows(client, storage_label: str) -> list[dict]:
        items = []
        for col in client.list_collections():
            repo = _collection_repo(col)
            repo_ns = _collection_namespace(col)
            if ns_filter is not None and repo_ns != ns_filter:
                continue
            items.append({
                "repo": repo,
                "namespace": repo_ns,
                "total_documents": col.count(),
                "storage": storage_label,
            })
        return items

    permanent = _rows(_persistent_client, "permanent")
    temporary = _rows(_ephemeral_client, "temporary")
    return permanent + temporary


# ── Indexing ──────────────────────────────────────────────────────────────────

def _complete_document_types(pr: dict) -> set[str]:
    """Identify document groups safe to reconcile after a partial GitHub response."""
    truncated = pr.get("truncated_connections")
    truncated_set = set(truncated) if isinstance(truncated, list) else set()
    complete = {"pr_description"}
    if not {"reviewThreads", "reviewThreads.comments"} & truncated_set:
        complete.add("review_comment")
    if "commits" not in truncated_set:
        complete.add("commit_message")
    if "reviews" not in truncated_set:
        complete.add("review_summary")
    return complete


def _stale_document_ids(collection, prs: list[dict], ids: list[str], metadatas: list[dict]) -> list[str]:
    """Find documents removed from complete portions of refreshed PR records."""
    current_ids: dict[tuple[int, str], set[str]] = {}
    for document_id, metadata in zip(ids, metadatas):
        pr_number = metadata.get("pr_number")
        doc_type = metadata.get("type")
        if isinstance(pr_number, int) and isinstance(doc_type, str):
            current_ids.setdefault((pr_number, doc_type), set()).add(document_id)

    stale: list[str] = []
    for pr in prs:
        pr_number = pr.get("number")
        if not isinstance(pr_number, int):
            continue
        complete_types = _complete_document_types(pr)
        existing = collection.get(where={"pr_number": pr_number}, include=["metadatas"])
        for existing_id, metadata in zip(
            existing.get("ids") or [], existing.get("metadatas") or []
        ):
            doc_type = metadata.get("type") if isinstance(metadata, dict) else None
            if (
                isinstance(doc_type, str)
                and doc_type in complete_types
                and existing_id not in current_ids.get((pr_number, doc_type), set())
            ):
                stale.append(existing_id)
    return stale


def index_prs(
    repo_key: str,
    prs: list[dict],
    temporary: bool = False,
    namespace: str | None = None,
    advance_watermark: bool = True,
    expected_epoch: int | None = None,
) -> int:
    """Upsert a successful GitHub batch, then atomically advance its watermark."""
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        collection = _get_collection(repo_key, temporary=temporary, namespace=namespace)
        docs, metadatas, ids = build_documents(prs)
        stale_ids = _stale_document_ids(collection, prs, ids, metadatas)

        ns = _normalize_namespace(namespace)
        for meta in metadatas:
            if ns:
                meta["namespace"] = ns

        if docs:
            embeddings = encode_batch(docs)
            collection.upsert(documents=docs, embeddings=embeddings, metadatas=metadatas, ids=ids)
        if stale_ids:
            collection.delete(ids=stale_ids)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        current_meta = collection.metadata or {}
        current_meta["last_indexed_at"] = now
        current_meta["last_checked_at"] = now
        update_meta = {key: value for key, value in current_meta.items() if key != "hnsw:space"}
        collection.modify(metadata=update_meta)

        # Advance only after all Chroma writes succeed. A failed embedding or
        # upsert therefore remains eligible for the next refresh.
        if prs:
            max_pr = max((pr.get("number", 0) for pr in prs), default=0)
            if max_pr > 0:
                from storage.cursor_store import cursor_store

                updated_at_values = [
                    value
                    for pr in prs
                    if isinstance((value := pr.get("updated_at")), str) and value
                ]
                cursor_store.set_cursor(
                    repo_key,
                    max_pr,
                    namespace=namespace,
                    storage="temporary" if temporary else "permanent",
                    last_updated_at=(
                        max(updated_at_values, default=None)
                        if advance_watermark
                        else None
                    ),
                )

    label = "temporary (in-memory)" if temporary else "permanent (disk)"
    print(f"Indexed {len(docs)} documents for {repo_key} [{label}]", file=sys.stderr)
    return len(docs)


def mark_repo_checked(
    repo_key: str,
    temporary: bool = False,
    namespace: str | None = None,
    expected_epoch: int | None = None,
) -> None:
    """Record a successful GitHub sync even if it produced no new documents."""
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        collection = _get_collection(repo_key, temporary=temporary, namespace=namespace)
        current_meta = collection.metadata or {}
        current_meta["last_checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        update_meta = {key: value for key, value in current_meta.items() if key != "hnsw:space"}
        collection.modify(metadata=update_meta)


def checkpoint_refresh(
    repo_key: str,
    *,
    temporary: bool = False,
    namespace: str | None = None,
    next_cursor: str,
    high_watermark: str | None,
    expected_epoch: int | None = None,
) -> None:
    """Persist a partial refresh only when its index scope is still current."""
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        from storage.cursor_store import cursor_store

        cursor_store.checkpoint_refresh(
            repo_key,
            namespace=namespace,
            storage="temporary" if temporary else "permanent",
            next_cursor=next_cursor,
            high_watermark=high_watermark,
        )


def complete_refresh(
    repo_key: str,
    last_pr_number: int,
    *,
    temporary: bool = False,
    namespace: str | None = None,
    high_watermark: str | None,
    expected_epoch: int | None = None,
) -> None:
    """Commit a completed refresh only when its index scope is still current."""
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        from storage.cursor_store import cursor_store

        cursor_store.complete_refresh(
            repo_key,
            last_pr_number,
            namespace=namespace,
            storage="temporary" if temporary else "permanent",
            high_watermark=high_watermark,
        )


# ── Querying ──────────────────────────────────────────────────────────────────

def query_similar(
    repo_key: str,
    query_text: str,
    n_results: int = 8,
    temporary: bool = False,
    namespace: str | None = None,
    where: dict | None = None,
) -> list[dict]:
    with _chroma_lock:
        collection = _get_collection(repo_key, temporary=temporary, namespace=namespace)
        total = collection.count()
        if total == 0:
            return []

        ns = _normalize_namespace(namespace)
        where_filter = dict(where or {})
        if ns and "namespace" not in where_filter:
            where_filter["namespace"] = ns

        # ChromaDB requires multiple filters to be wrapped in $and
        final_where = None
        if len(where_filter) == 1:
            final_where = where_filter
        elif len(where_filter) > 1:
            final_where = {"$and": [{k: v} for k, v in where_filter.items()]}

        results = collection.query(
            query_embeddings=[encode(query_text)],
            n_results=n_results,
            where=final_where,
            include=["documents", "metadatas", "distances"],
        )

    if not results["documents"] or not results["documents"][0]:
        return []

    return [
        {
            "text": doc,
            "metadata": {
                k: v for k, v in meta.items() 
                if k not in {"namespace", "files", "author_is_bot", "is_bot"}
            },
            "similarity": round(1 - dist, 4),
        }
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )
    ]


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_collection_stats(
    repo_key: str,
    temporary: bool = False,
    namespace: str | None = None,
) -> dict:
    with _chroma_lock:
        collection = _get_collection(repo_key, temporary=temporary, namespace=namespace)
        ns = _normalize_namespace(namespace)
        where_filter = {"namespace": ns} if ns else None
        
        try:
            data = collection.get(where=where_filter, include=[])
            count = len(data["ids"]) if data and "ids" in data else 0
        except Exception:
            count = 0

        meta = collection.metadata or {}
        last_indexed = meta.get("last_indexed_at")
        last_checked = meta.get("last_checked_at") or last_indexed
        
        is_stale = False
        days_old = None
        if last_checked:
            try:
                last_dt = datetime.datetime.fromisoformat(last_checked)
                delta = datetime.datetime.now(datetime.timezone.utc) - last_dt
                days_old = delta.days
                is_stale = days_old > 30
            except Exception:
                pass

    return {
        "repo": repo_key,
        "namespace": ns,
        "total_documents": count,
        "storage": "temporary" if temporary else "permanent",
        "last_indexed_at": last_indexed,
        "last_checked_at": last_checked,
        "days_old": days_old,
        "is_stale": is_stale,
    }


# ── Deletion ──────────────────────────────────────────────────────────────────

def delete_repo_index(
    repo_key: str,
    storage: str = "both",
    namespace: str | None = None,
) -> dict:
    if storage not in {"temporary", "permanent", "both"}:
        raise ValueError("storage must be one of: temporary, permanent, both")

    name = _collection_name(repo_key, namespace=namespace)
    ns = _normalize_namespace(namespace)

    deleted = {"temporary": False, "permanent": False}

    with _chroma_lock:
        if storage in {"temporary", "both"}:
            key = _index_scope_key(repo_key, True, namespace)
            _INDEX_EPOCHS[key] = _INDEX_EPOCHS.get(key, 0) + 1
            try:
                _ephemeral_client.delete_collection(name)
                deleted["temporary"] = True
            except Exception:
                pass

        if storage in {"permanent", "both"}:
            key = _index_scope_key(repo_key, False, namespace)
            _INDEX_EPOCHS[key] = _INDEX_EPOCHS.get(key, 0) + 1
            try:
                _persistent_client.delete_collection(name)
                deleted["permanent"] = True
            except Exception:
                pass

        from storage.cursor_store import cursor_store
        if storage in {"temporary", "both"}:
            cursor_store.clear_cursor(repo_key, namespace=namespace, storage="temporary")
        if storage in {"permanent", "both"}:
            cursor_store.clear_cursor(repo_key, namespace=namespace, storage="permanent")

    return {
        "repo": repo_key,
        "namespace": ns,
        "storage": storage,
        "deleted": deleted,
        "deleted_any": any(deleted.values()),
    }
