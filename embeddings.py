import chromadb
from sentence_transformers import SentenceTransformer
import os
import json
from dotenv import load_dotenv

load_dotenv()

PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")

# Load once at module level — expensive to reload
model = SentenceTransformer("all-MiniLM-L6-v2")

client = chromadb.PersistentClient(path=PERSIST_DIR)

def get_collection(repo_key: str):
    # ChromaDB collection names must be alphanumeric + hyphens
    safe_name = repo_key.replace("/", "--")
    return client.get_or_create_collection(
        name=safe_name,
        metadata={"hnsw:space": "cosine"},
    )

def index_prs(repo_key: str, prs: list[dict]):
    """Embed and store all PR review comments + PR descriptions."""
    collection = get_collection(repo_key)

    docs, embeddings, metadatas, ids = [], [], [], []

    for pr in prs:
        pr_num = pr["number"]

        # Index PR description
        if pr["body"].strip():
            text = f"PR #{pr_num}: {pr['title']}\n{pr['body']}"
            docs.append(text)
            embeddings.append(model.encode(text).tolist())
            metadatas.append({
                "type": "pr_description",
                "pr_number": pr_num,
                "author": pr["author"],
                "files": json.dumps([f["path"] for f in pr["files"]]),
            })
            ids.append(f"pr-{pr_num}-desc")

        # Index each review comment
        for i, comment in enumerate(pr["review_comments"]):
            if not comment["body"].strip():
                continue
            text = (
                f"PR #{pr_num} | File: {comment['file']} | Line: {comment['line']}\n"
                f"Reviewer ({comment['author']}): {comment['body']}"
            )
            docs.append(text)
            embeddings.append(model.encode(text).tolist())
            metadatas.append({
                "type": "review_comment",
                "pr_number": pr_num,
                "file": comment["file"],
                "author": comment["author"],
                "resolved": comment["resolved"],
            })
            ids.append(f"pr-{pr_num}-comment-{i}")

        # Index overall reviews (APPROVED / CHANGES_REQUESTED with body)
        for i, review in enumerate(pr["reviews"]):
            if not review["body"].strip():
                continue
            text = (
                f"PR #{pr_num} overall review by {review['author']} "
                f"[{review['state']}]: {review['body']}"
            )
            docs.append(text)
            embeddings.append(model.encode(text).tolist())
            metadatas.append({
                "type": "review_summary",
                "pr_number": pr_num,
                "state": review["state"],
                "author": review["author"],
            })
            ids.append(f"pr-{pr_num}-review-{i}")

    if docs:
        # Upsert handles re-indexing cleanly
        collection.upsert(
            documents=docs,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids,
        )

    print(f"Indexed {len(docs)} documents for {repo_key}")
    return len(docs)

def query_similar(repo_key: str, query_text: str, n_results: int = 8) -> list[dict]:
    """Find most relevant past review comments for a query."""
    collection = get_collection(repo_key)
    query_embedding = model.encode(query_text).tolist()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(n_results, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    output = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        output.append({
            "text": doc,
            "metadata": meta,
            "similarity": round(1 - dist, 4),  # cosine: distance → similarity
        })

    return output

def get_collection_stats(repo_key: str) -> dict:
    collection = get_collection(repo_key)
    return {"repo": repo_key, "total_documents": collection.count()}

def list_all_repos() -> list[dict]:
    """List all repos currently indexed in ChromaDB with their document counts."""
    collections = client.list_collections()
    result = []
    for col in collections:
        # Reverse the owner--repo encoding back to owner/repo
        repo_key = col.name.replace("--", "/")
        result.append({
            "repo": repo_key,
            "total_documents": col.count(),
        })
    return result
