"""Retrieval hits expose links and author type; the bot filter works on a real Chroma."""

import pytest

pytest.importorskip("chromadb", reason="ChromaDB is required for vector-store integration tests")

import storage.vector_store as vector_store
from storage.vector_store import delete_repo_index, index_prs, query_similar

REPO = "acme/widgets"
BASE = "https://github.com/acme/widgets/pull"


@pytest.fixture(autouse=True)
def offline_encoder(monkeypatch):
    monkeypatch.setattr(vector_store, "encode", lambda text: [1.0, 0.0])
    monkeypatch.setattr(vector_store, "encode_batch", lambda texts: [[1.0, 0.0] for _ in texts])


@pytest.fixture
def namespace(request):
    name = f"retrieval-{request.node.name}"
    yield name
    delete_repo_index(REPO, storage="temporary", namespace=name)


def _pr(number, author, author_type, body):
    return {
        "number": number, "url": f"{BASE}/{number}", "title": f"PR {number}", "body": body,
        "author": author, "author_is_bot": author_type == "bot", "author_type": author_type,
        "updated_at": "2024-01-01T00:00:00Z", "touches_ci": False, "files": [],
        "review_comments": [], "commits": [], "reviews": [],
    }


def test_hits_carry_links_author_type_and_hide_internal_flags(namespace):
    pr = _pr(1, "alice", "human", "A long enough description of the change.")
    pr["review_comments"] = [{
        "github_node_id": "C1", "url": f"{BASE}/1#discussion_r5",
        "review_url": f"{BASE}/1#pullrequestreview-4", "file": "a.py", "line": 1,
        "resolved": False, "author": "bob", "is_bot": False, "author_type": "human",
        "body": "Please add a regression test for this branch.", "diff_hunk": "",
    }]
    index_prs(REPO, [pr], temporary=True, namespace=namespace)

    hits = {h["metadata"]["type"]: h for h in query_similar(REPO, "q", temporary=True, namespace=namespace)}
    assert hits["review_comment"]["links"] == {
        "pr": f"{BASE}/1",
        "review": f"{BASE}/1#pullrequestreview-4",
        "comment": f"{BASE}/1#discussion_r5",
    }
    assert hits["pr_description"]["links"] == {"pr": f"{BASE}/1", "review": None, "comment": None}
    assert hits["review_comment"]["author_type"] == "human"
    for hidden in ("pr_url", "review_url", "comment_url", "author_type", "is_bot", "author_is_bot"):
        assert hidden not in hits["review_comment"]["metadata"]


def test_exclude_bots_drops_bot_documents_and_composes_with_other_filters(namespace):
    prs = [
        _pr(1, "alice", "human", "Human written description of the change."),
        _pr(2, "dependabot", "bot", "Bot written description of the upgrade."),
    ]
    prs[1]["touches_ci"] = True
    index_prs(REPO, prs, temporary=True, namespace=namespace)

    everyone = query_similar(REPO, "q", temporary=True, namespace=namespace)
    humans = query_similar(REPO, "q", temporary=True, namespace=namespace, exclude_bots=True)
    assert {h["author_type"] for h in everyone} == {"human", "bot"}
    assert [h["author_type"] for h in humans] == ["human"]
    only_bot_ci = query_similar(
        REPO, "q", temporary=True, namespace=namespace,
        where={"touches_ci": True}, exclude_bots=True,
    )
    assert only_bot_ci == []


def test_documents_indexed_before_author_types_existed_survive_the_filter(namespace):
    index_prs(REPO, [_pr(1, "alice", "human", "A description indexed by an older version.")],
              temporary=True, namespace=namespace)
    collection = vector_store._get_collection(REPO, temporary=True, namespace=namespace)
    stored = collection.get(include=["metadatas", "documents"])
    # Chroma merges metadata on update, so rebuild each document the way an
    # older version stored it: a bare is_bot flag, no author_type, no links.
    collection.delete(ids=stored["ids"])
    new_keys = {"author_type", "pr_url", "review_url", "comment_url"}
    for doc_id, doc, meta in zip(stored["ids"], stored["documents"], stored["metadatas"]):
        legacy = {k: v for k, v in meta.items() if k not in new_keys}
        legacy["author_is_bot"] = False
        collection.add(ids=[doc_id], documents=[doc], embeddings=[[1.0, 0.0]], metadatas=[legacy])
    assert "author_type" not in collection.get(include=["metadatas"])["metadatas"][0]

    humans = query_similar(REPO, "q", temporary=True, namespace=namespace, exclude_bots=True)
    assert len(humans) == 1
    assert humans[0]["author_type"] == "human"          # derived from the legacy flag
    assert humans[0]["links"] == {"pr": None, "review": None, "comment": None}


def test_stats_include_stored_bounds_only_when_documents_exist(namespace):
    empty = vector_store.get_collection_stats(REPO, temporary=True, namespace=namespace)
    assert empty["bounds"] == {"recorded": False}

    index_prs(
        REPO, [_pr(3, "alice", "human", "Description long enough to be indexed.")],
        temporary=True, namespace=namespace, history_truncated=True, pr_limit=30, pages=1,
    )
    try:
        stats = vector_store.get_collection_stats(REPO, temporary=True, namespace=namespace)
        assert stats["bounds"]["indexed_pr_count"] == 1
        assert stats["bounds"]["history_truncated"] is True
        assert stats["bounds"]["configured_pr_limit"] == 30
    finally:
        from storage.cursor_store import cursor_store
        cursor_store.clear_cursor(REPO, namespace=namespace, storage="temporary")


def test_exclude_bots_widens_the_search_past_nearer_bot_documents(namespace, monkeypatch):
    # Bot documents sit exactly on the query; the lone human one is farther away.
    monkeypatch.setattr(
        vector_store, "encode_batch",
        lambda texts: [[0.0, 1.0] if "Human" in text else [1.0, 0.0] for text in texts],
    )
    prs = [_pr(n, "dependabot", "bot", f"Bot description number {n} of the upgrade.") for n in range(1, 7)]
    prs.append(_pr(7, "alice", "human", "Human description of the real change."))
    index_prs(REPO, prs, temporary=True, namespace=namespace)

    nearest = query_similar(REPO, "q", n_results=2, temporary=True, namespace=namespace)
    assert {h["author_type"] for h in nearest} == {"bot"}

    humans = query_similar(REPO, "q", n_results=2, temporary=True, namespace=namespace, exclude_bots=True)
    assert [h["metadata"]["pr_number"] for h in humans] == [7]
