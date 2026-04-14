# ChromaDB client management, indexing, querying, and repo listing.
# No ML model loading, no PR transformation, no GitHub calls here.

import chromadb
import os
from dotenv import load_dotenv
from storage.encoder import encode
from storage.document_builder import build_documents

load_dotenv()

PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")

# Persistent = survives restarts, stored on disk
_persistent_client = chromadb.PersistentClient(path=PERSIST_DIR)

# Ephemeral = in-memory only, wiped when the MCP server process stops
_ephemeral_client = chromadb.EphemeralClient()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _safe_name(repo_key: str) -> str:
    return repo_key.replace("/", "--")

def _client(temporary: bool):
    return _ephemeral_client if temporary else _persistent_client

def _get_collection(repo_key: str, temporary: bool = False):
    return _client(temporary).get_or_create_collection(
        name=_safe_name(repo_key),
        metadata={"hnsw:space": "cosine"},
    )


# ── Status checks ─────────────────────────────────────────────────────────────

def repo_is_indexed_permanently(repo_key: str) -> bool:
    try:
        col = _persistent_client.get_collection(_safe_name(repo_key))
        return col.count() > 0
    except Exception:
        return False

def repo_is_indexed_temporarily(repo_key: str) -> bool:
    try:
        col = _ephemeral_client.get_collection(_safe_name(repo_key))
        return col.count() > 0
    except Exception:
        return False


# ── Listing ───────────────────────────────────────────────────────────────────

def list_all_repos() -> list[dict]:
    permanent = [
        {"repo": col.name.replace("--", "/"), "total_documents": col.count(), "storage": "permanent"}
        for col in _persistent_client.list_collections()
    ]
    temporary = [
        {"repo": col.name.replace("--", "/"), "total_documents": col.count(), "storage": "temporary"}
        for col in _ephemeral_client.list_collections()
    ]
    return permanent + temporary


# ── Indexing ──────────────────────────────────────────────────────────────────

def index_prs(repo_key: str, prs: list[dict], temporary: bool = False) -> int:
    """
    Embed and store all PR documents.
    temporary=False → persistent on-disk ChromaDB
    temporary=True  → ephemeral in-memory (lost on server restart)
    """
    collection = _get_collection(repo_key, temporary=temporary)
    docs, metadatas, ids = build_documents(prs)

    if not docs:
        return 0

    embeddings = [encode(doc) for doc in docs]
    collection.upsert(documents=docs, embeddings=embeddings, metadatas=metadatas, ids=ids)

    label = "temporary (in-memory)" if temporary else "permanent (disk)"
    print(f"Indexed {len(docs)} documents for {repo_key} [{label}]")
    return len(docs)


# ── Querying ──────────────────────────────────────────────────────────────────

def query_similar(
    repo_key: str,
    query_text: str,
    n_results: int = 8,
    temporary: bool = False,
) -> list[dict]:
    collection = _get_collection(repo_key, temporary=temporary)
    total = collection.count()
    if total == 0:
        return []

    results = collection.query(
        query_embeddings=[encode(query_text)],
        n_results=min(n_results, total),
        include=["documents", "metadatas", "distances"],
    )

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

def get_collection_stats(repo_key: str, temporary: bool = False) -> dict:
    collection = _get_collection(repo_key, temporary=temporary)
    return {
        "repo": repo_key,
        "total_documents": collection.count(),
        "storage": "temporary" if temporary else "permanent",
    }
