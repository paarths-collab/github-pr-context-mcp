# ChromaDB client management, indexing, querying, and repo listing.
# No ML model loading, no PR transformation, no GitHub calls here.

import chromadb
import os
import re
import sys
import threading
import datetime
from dotenv import load_dotenv
from storage.encoder import encode
from storage.document_builder import build_documents

load_dotenv()

_DEFAULT_CHROMA_DIR = os.path.join(os.path.expanduser("~"), ".github-pr-mcp", "chroma_db")
PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", _DEFAULT_CHROMA_DIR)

# Persistent = survives restarts, stored on disk
_persistent_client = chromadb.PersistentClient(path=PERSIST_DIR)

# Ephemeral = in-memory only, wiped when the MCP server process stops
_ephemeral_client = chromadb.EphemeralClient()

_chroma_lock = threading.Lock()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize_namespace(namespace: str | None) -> str | None:
    if namespace is None:
        return None
    ns = namespace.strip()
    return ns or None

def _safe_name(repo_key: str) -> str:
    return repo_key.replace("/", "--")

def _collection_name(repo_key: str, namespace: str | None = None) -> str:
    return _safe_name(repo_key)

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

def index_prs(
    repo_key: str,
    prs: list[dict],
    temporary: bool = False,
    namespace: str | None = None,
) -> int:
    with _chroma_lock:
        collection = _get_collection(repo_key, temporary=temporary, namespace=namespace)
        docs, metadatas, ids = build_documents(prs)

        if not docs:
            return 0

        ns = _normalize_namespace(namespace)
        for meta in metadatas:
            if ns:
                meta["namespace"] = ns

        embeddings = [encode(doc) for doc in docs]
        collection.upsert(documents=docs, embeddings=embeddings, metadatas=metadatas, ids=ids)

        # Update cursor store with highest PR number
        if prs:
            max_pr = max(pr.get("number", 0) for pr in prs)
            if max_pr > 0:
                from storage.cursor_store import cursor_store
                cursor_store.set_cursor(repo_key, max_pr, namespace=namespace)

        # Update collection metadata
        current_meta = collection.metadata or {}
        current_meta["last_indexed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        # Filter out immutable keys
        update_meta = {k: v for k, v in current_meta.items() if k != "hnsw:space"}
        collection.modify(metadata=update_meta)

    label = "temporary (in-memory)" if temporary else "permanent (disk)"
    print(f"Indexed {len(docs)} documents for {repo_key} [{label}]", file=sys.stderr)
    return len(docs)


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
        where_filter = where or {}
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
            "metadata": meta,
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
        
        is_stale = False
        days_old = None
        if last_indexed:
            try:
                last_dt = datetime.datetime.fromisoformat(last_indexed)
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
    where_filter = {"namespace": ns} if ns else None

    deleted = {"temporary": False, "permanent": False}

    with _chroma_lock:
        if storage in {"temporary", "both"}:
            try:
                col = _ephemeral_client.get_collection(name)
                if where_filter:
                    col.delete(where=where_filter)
                else:
                    _ephemeral_client.delete_collection(name)
                deleted["temporary"] = True
            except Exception:
                pass

        if storage in {"permanent", "both"}:
            try:
                col = _persistent_client.get_collection(name)
                if where_filter:
                    col.delete(where=where_filter)
                else:
                    _persistent_client.delete_collection(name)
                deleted["permanent"] = True
            except Exception:
                pass

    return {
        "repo": repo_key,
        "namespace": ns,
        "storage": storage,
        "deleted": deleted,
        "deleted_any": any(deleted.values()),
    }
