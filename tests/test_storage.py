import pytest
import datetime

pytest.importorskip("chromadb", reason="ChromaDB is required for vector-store integration tests")

import storage.vector_store as vector_store
from storage.vector_store import index_prs, query_similar, get_collection_stats, delete_repo_index

def test_chromadb_roundtrip_with_metadata(monkeypatch):
    # This is a Chroma contract test, not a model-download integration test.
    # Keep CI offline and deterministic by substituting a tiny fixed encoder.
    monkeypatch.setattr(vector_store, "encode", lambda text: [0.0])
    monkeypatch.setattr(vector_store, "encode_batch", lambda texts: [[0.0] for _ in texts])
    repo = "test-owner/test-repo"
    namespace = "test-ns"
    
    prs = [
        {
            "number": 1,
            "title": "CI Fix",
            "body": "Fixed the dockerfile",
            "author": "dev-user",
            "author_is_bot": False,
            "touches_ci": True,
            "files": [{"path": "Dockerfile"}],
            "review_comments": [
                {
                    "file": "Dockerfile",
                    "line": 1,
                    "resolved": True,
                    "author": "senior-reviewer",
                    "is_bot": False,
                    "body": "Good use of multi-stage builds",
                    "created_at": "2024-05-01T10:00:00Z"
                }
            ],
            "reviews": []
        }
    ]
    
    # Use temporary storage for testing
    count = index_prs(repo, prs, temporary=True, namespace=namespace)
    assert count > 0
    
    # Test metadata filtering (Priority 3)
    results = query_similar(repo, "docker", temporary=True, namespace=namespace, where={"touches_ci": True})
    assert len(results) > 0
    assert results[0]["metadata"]["touches_ci"] is True
    
    # Test bot filtering (Priority 5)
    results_no_bot = query_similar(repo, "multi-stage", temporary=True, namespace=namespace, where={"is_bot": False})
    assert len(results_no_bot) > 0
    
    # Test stats (Priority 1)
    stats = get_collection_stats(repo, temporary=True, namespace=namespace)
    assert stats["total_documents"] > 0
    assert stats["last_indexed_at"] is not None
    assert stats["is_stale"] is False
    
    # Cleanup
    delete_repo_index(repo, storage="temporary", namespace=namespace)
