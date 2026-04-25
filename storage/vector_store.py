# ChromaDB client management, indexing, querying, and repo listing.
# No ML model loading, no PR transformation, no GitHub calls here.

import chromadb
import os
import hashlib
import re
import sys
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


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize_namespace(namespace: str | None) -> str | None:
    if namespace is None:
        return None
    ns = namespace.strip()
    return ns or None

def _safe_namespace(namespace: str | None) -> str | None:
    ns = _normalize_namespace(namespace)
    if ns is None:
        return None
    # Keep names portable across Chroma backends.
    return re.sub(r"[^A-Za-z0-9_-]", "-", ns)

def _safe_name(repo_key: str) -> str:
    return repo_key.replace("/", "--")

def _collection_name(repo_key: str, namespace: str | None = None) -> str:
    # We now strictly use ONE collection per repository to preserve ChromaDB capacity.
    # User isolation is handled by injecting the namespace into document metadata and applying `where` filters.
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
    # Backward compatibility for collections created before metadata tagging.
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
    """
    Embed and store all PR documents.
    temporary=False → persistent on-disk ChromaDB
    temporary=True  → ephemeral in-memory (lost on server restart)
    """
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

    label = "temporary (in-memory)" if temporary else "permanent (disk)"
    ns = _normalize_namespace(namespace)
    ns_suffix = f", namespace={ns}" if ns else ""
    print(f"Indexed {len(docs)} documents for {repo_key} [{label}{ns_suffix}]", file=sys.stderr)
    return len(docs)


# ── Querying ──────────────────────────────────────────────────────────────────

def query_similar(
    repo_key: str,
    query_text: str,
    n_results: int = 8,
    temporary: bool = False,
    namespace: str | None = None,
) -> list[dict]:
    collection = _get_collection(repo_key, temporary=temporary, namespace=namespace)
    total = collection.count()
    if total == 0:
        return []

    ns = _normalize_namespace(namespace)
    where_filter = {"namespace": ns} if ns else None

    # We must explicitly query with a where_filter to isolate queries to this namespace's vectors
    results = collection.query(
        query_embeddings=[encode(query_text)],
        n_results=n_results, # We might get fewer than n_results back, which is fine
        where=where_filter,
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
    collection = _get_collection(repo_key, temporary=temporary, namespace=namespace)
    ns = _normalize_namespace(namespace)
    where_filter = {"namespace": ns} if ns else None
    
    try:
        data = collection.get(where=where_filter, include=[])
        count = len(data["ids"]) if data and "ids" in data else 0
    except Exception:
        count = 0

    return {
        "repo": repo_key,
        "namespace": ns,
        "total_documents": count,
        "storage": "temporary" if temporary else "permanent",
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

    deleted = {
        "temporary": False,
        "permanent": False,
    }

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
        "namespace": _normalize_namespace(namespace),
        "storage": storage,
        "deleted": deleted,
        "deleted_any": any(deleted.values()),
    }
