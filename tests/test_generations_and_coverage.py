"""Generation publishing, coverage, lexical retrieval and delete/epoch behaviour.

Uses real Chroma (a tmp-dir persistent client + the shared ephemeral client), a
real tmp SQLite registry, a deterministic fake encoder and a fake document
builder so the tests do not depend on the model or on document_builder.
"""

import hashlib
import importlib
import json
import sqlite3
import uuid

import pytest

pytest.importorskip("chromadb", reason="ChromaDB is required for vector-store integration tests")

import chromadb  # noqa: E402

import storage.vector_store as vs  # noqa: E402
from storage.cursor_store import CursorStore  # noqa: E402

# ``storage.cursor_store`` the attribute is the singleton instance; fetch the module.
cursor_module = importlib.import_module("storage.cursor_store")


def _vector(text):
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [(b + 1) / 256.0 for b in digest[:8]]
    return values


class _Env:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.db_path = tmp_path / "cursors.db"
        self.encoded_batches = []

    def reopen_registry(self):
        """Simulate a process restart: a fresh CursorStore on the same file."""
        store = CursorStore(str(self.db_path))
        cursor_module.cursor_store = store
        return store


def _fake_build_documents(prs):
    docs, metas, ids = [], [], []
    for pr in prs:
        for d in pr.get("documents", []):
            docs.append(d["text"])
            meta = {
                "type": d.get("type", "review_thread"),
                "pr_number": pr["number"],
                "pr_created_at": pr.get("created_at", ""),
                "pr_updated_at": pr.get("updated_at", ""),
                "pr_state": pr.get("state", ""),
                "source_kind": d.get("source_kind", "human_review_thread"),
                "is_bot": d.get("is_bot", False),
                "author_type": "bot" if d.get("is_bot") else "human",
                "source_url": d.get("source_url", f"https://example.test/pr/{pr['number']}"),
                "original_path": d.get("path", ""),
                "thread_evidence_id": d.get("thread_evidence_id", ""),
                "part_index": d.get("part_index", 0),
                "part_count": d.get("part_count", 1),
                "thread_incomplete": d.get("thread_incomplete", False),
                "evidence_id": d["id"],
            }
            if "truncated_connections" in d:
                meta["truncated_connections"] = json.dumps(d["truncated_connections"])
            metas.append(meta)
            ids.append(d["id"])
    return docs, metas, ids


@pytest.fixture
def env(monkeypatch, tmp_path):
    e = _Env(tmp_path)
    store = CursorStore(str(e.db_path))
    monkeypatch.setattr(cursor_module, "cursor_store", store)
    monkeypatch.setattr(vs, "_persistent_client", chromadb.PersistentClient(path=str(tmp_path / "chroma")))

    def encode_batch(texts):
        e.encoded_batches.append(list(texts))
        return [_vector(t) for t in texts]

    monkeypatch.setattr(vs, "encode", _vector)
    monkeypatch.setattr(vs, "encode_batch", encode_batch)
    monkeypatch.setattr(vs, "build_documents", _fake_build_documents)
    return e


def _pr(number, texts, *, created="2024-03-01T00:00:00Z", state="MERGED", **doc_extra):
    return {
        "number": number,
        "created_at": created,
        "updated_at": created,
        "state": state,
        "nested_incomplete_threads": sum(1 for t in texts if isinstance(t, dict) and t.get("thread_incomplete")),
        "documents": [
            (t if isinstance(t, dict) else {"text": t, **doc_extra}) | {"id": f"pr{number}-d{i}"}
            for i, t in enumerate(texts)
        ],
    }


def _names(client):
    return sorted(c if isinstance(c, str) else c.name for c in client.list_collections())


def _scope_collections(repo, client=None):
    client = client or vs._persistent_client
    base = vs._generation_base(repo)
    return [n for n in _names(client) if n.startswith(base) or n == vs._collection_name(repo)]


# ── generation publish ────────────────────────────────────────────────────────

def test_generation_publish_copies_without_reencoding_and_keeps_only_previous(env):
    repo = "acme/gen-publish"
    vs.index_prs(repo, [_pr(1, ["use retries for flaky network calls"])])
    assert vs.get_active_generation(repo) == 1
    assert _scope_collections(repo) == [vs._generation_name(repo, 1)]

    env.encoded_batches.clear()
    vs.index_prs(repo, [_pr(2, ["prefer composition over inheritance here"])])
    # Only the NEW document is encoded; PR 1 is copied with its stored embedding.
    assert env.encoded_batches == [["prefer composition over inheritance here"]]
    assert vs.get_active_generation(repo) == 2
    assert _scope_collections(repo) == [vs._generation_name(repo, 1), vs._generation_name(repo, 2)]
    active = vs._get_collection(repo)
    copied = active.get(ids=["pr1-d0"], include=["embeddings", "documents"])
    assert list(copied["embeddings"][0]) == pytest.approx(_vector("use retries for flaky network calls"))
    assert active.count() == 2

    vs.index_prs(repo, [_pr(3, ["pin the docker base image digest"])])
    assert vs.get_active_generation(repo) == 3
    assert _scope_collections(repo) == [vs._generation_name(repo, 2), vs._generation_name(repo, 3)]
    assert vs.get_collection_stats(repo)["total_documents"] == 3
    assert [r["pr_number"] for r in cursor_module.cursor_store.get_ledger(repo)] == [1, 2, 3]
    assert vs.repo_is_indexed_permanently(repo)


def test_publish_reconciles_deletes_in_the_staged_generation_only(env):
    repo = "acme/reconcile"
    vs.index_prs(repo, [_pr(1, ["first body", "second body"])])
    vs.index_prs(repo, [_pr(1, ["first body"])])
    assert vs._get_collection(repo).get(include=[])["ids"] == ["pr1-d0"]
    # The previous generation still holds both docs until it is pruned.
    previous = vs._persistent_client.get_collection(vs._generation_name(repo, 1))
    assert sorted(previous.get(include=[])["ids"]) == ["pr1-d0", "pr1-d1"]


def test_failed_staging_keeps_previous_generation_serving_and_removes_staged(env, monkeypatch):
    repo = "acme/fail-stage"
    vs.index_prs(repo, [_pr(1, ["existing guidance about logging"])])
    before = vs.query_similar(repo, "logging", n_results=3)
    assert before

    def boom(*args, **kwargs):
        raise RuntimeError("staging exploded")

    monkeypatch.setattr(vs, "_stale_document_ids", boom)
    with pytest.raises(RuntimeError, match="staging exploded"):
        vs.index_prs(repo, [_pr(2, ["new guidance that must not appear"])])

    assert vs.get_active_generation(repo) == 1
    assert _scope_collections(repo) == [vs._generation_name(repo, 1)]
    after = vs.query_similar(repo, "logging", n_results=3)
    assert [r["id"] for r in after] == [r["id"] for r in before]
    assert vs.get_collection_stats(repo)["total_documents"] == 1


def test_failure_at_publish_time_cleans_up_and_leaves_registry_untouched(env, monkeypatch):
    repo = "acme/fail-publish"
    vs.index_prs(repo, [_pr(1, ["alpha doc"])])

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cursor_module.cursor_store, "publish_generation", refuse)
    with pytest.raises(sqlite3.OperationalError):
        vs.index_prs(repo, [_pr(2, ["beta doc"])])
    assert vs.get_active_generation(repo) == 1
    assert _scope_collections(repo) == [vs._generation_name(repo, 1)]
    assert vs.get_collection_stats(repo)["total_documents"] == 1


def test_failed_embedding_creates_no_staged_collection(env, monkeypatch):
    repo = "acme/fail-embed"
    vs.index_prs(repo, [_pr(1, ["alpha doc"])])

    def fail(texts):
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(vs, "encode_batch", fail)
    with pytest.raises(RuntimeError, match="embeddings unavailable"):
        vs.index_prs(repo, [_pr(2, ["beta doc"])])
    assert _scope_collections(repo) == [vs._generation_name(repo, 1)]


def test_orphan_staged_collection_from_a_crash_is_ignored_and_swept(env):
    repo = "acme/orphan"
    vs.index_prs(repo, [_pr(1, ["alpha doc"])])
    # A crash left an unpublished g2 behind; readers must keep using g1.
    vs._persistent_client.get_or_create_collection(
        name=vs._generation_name(repo, 2), metadata=vs._collection_metadata(repo)
    )
    assert vs.get_active_generation(repo) == 1
    assert vs.get_collection_stats(repo)["total_documents"] == 1
    vs.index_prs(repo, [_pr(2, ["beta doc"])])
    assert vs.get_active_generation(repo) == 3  # never reuses the orphan's number
    assert vs.get_collection_stats(repo)["total_documents"] == 2


# ── legacy collection ─────────────────────────────────────────────────────────

def _seed_legacy(repo, client=None, *, truncated=False):
    client = client or vs._persistent_client
    col = client.get_or_create_collection(
        name=vs._collection_name(repo), metadata=vs._collection_metadata(repo)
    )
    meta = {
        "type": "review_comment",
        "pr_number": 7,
        "pr_created_at": "2023-01-05T00:00:00Z",
        "pr_state": "MERGED",
    }
    if truncated:
        meta["truncated_connections"] = json.dumps(["pullRequests"])
    col.upsert(
        ids=["legacy-1"],
        documents=["legacy guidance about error handling"],
        embeddings=[_vector("legacy guidance about error handling")],
        metadatas=[meta],
    )
    return col


def test_legacy_unsuffixed_collection_is_readable_and_superseded_by_first_write(env):
    repo = "acme/legacy-read"
    _seed_legacy(repo, truncated=True)

    assert vs.repo_is_indexed_permanently(repo)
    stats = vs.get_collection_stats(repo)
    assert (stats["generation"], stats["legacy_index"], stats["total_documents"]) == (0, True, 1)
    hits = vs.query_similar(repo, "error handling", n_results=3)
    assert [h["id"] for h in hits] == ["legacy-1"]
    assert {r["repo"]: r["generation"] for r in vs.list_all_repos() if r["repo"] == repo} == {repo: 0}

    coverage = vs.compute_coverage(repo)
    assert coverage["history_complete"] is None
    assert coverage["older_history_remaining"] is True
    assert coverage["pull_requests_indexed"] == 1
    assert (coverage["pr_number_min"], coverage["pr_number_max"]) == (7, 7)
    assert coverage["oldest_pr_created_at"] == "2023-01-05T00:00:00Z"
    assert "no coverage ledger" in coverage["summary"]

    env.encoded_batches.clear()
    vs.index_prs(repo, [_pr(8, ["fresh guidance"])])
    assert env.encoded_batches == [["fresh guidance"]]  # legacy doc not re-encoded
    assert vs.get_active_generation(repo) == 1
    assert vs.get_collection_stats(repo)["total_documents"] == 2
    # Legacy collection is the immediately previous ready generation: kept once...
    assert vs._collection_name(repo) in _scope_collections(repo)
    # ...and the ledger was derived from its metadata, so coverage keeps PR 7.
    coverage = vs.compute_coverage(repo)
    assert (coverage["pull_requests_indexed"], coverage["pr_number_min"], coverage["pr_number_max"]) == (2, 7, 8)
    vs.index_prs(repo, [_pr(9, ["even fresher guidance"])])
    assert vs._collection_name(repo) not in _scope_collections(repo)


def test_legacy_without_truncation_marker_does_not_claim_older_history(env):
    repo = "acme/legacy-clean"
    _seed_legacy(repo, truncated=False)
    coverage = vs.compute_coverage(repo)
    assert coverage["history_complete"] is None
    # Unknown completeness counts as "older history may remain" (changed from
    # False: an unknown state must never read as "nothing left to fetch").
    assert coverage["older_history_remaining"] is True
    assert "bounded history window, not full repository history" in coverage["summary"]
    assert "history completeness is unknown" in coverage["summary"]


# ── coverage ──────────────────────────────────────────────────────────────────

def test_coverage_counts_range_window_and_survives_restart(env):
    repo = "acme/coverage"
    vs.index_prs(
        repo,
        [
            _pr(10, ["a one", "a two"], created="2024-02-10T08:00:00Z", state="MERGED"),
            _pr(12, ["b one"], created="2024-03-01T09:00:00Z", state="CLOSED"),
            _pr(11, [{"text": "c one", "thread_incomplete": True}], created="2024-02-20T00:00:00Z"),
        ],
    )
    vs.set_backfill_state(repo, backfill_cursor="cursor-after-12", history_complete=False, add_pages=1)

    def check():
        cov = vs.compute_coverage(repo)
        assert cov["pull_requests_indexed"] == 3
        assert (cov["pr_number_min"], cov["pr_number_max"]) == (10, 12)
        assert cov["oldest_pr_created_at"] == "2024-02-10T08:00:00Z"
        assert cov["newest_pr_created_at"] == "2024-03-01T09:00:00Z"
        assert cov["pr_states_indexed"] == ["CLOSED", "MERGED"]
        assert cov["evidence_units"] == 4
        assert cov["nested_incomplete_units"] == 1
        assert cov["history_complete"] is False
        assert cov["older_history_remaining"] is True
        assert cov["selection"] == "closed-created-window"
        assert cov["generation"] == 1
        assert cov["checked_at"]
        summary = cov["summary"]
        assert summary.count(".") == 1 and summary.endswith(".")
        for fragment in (
            "3 closed pull requests", "#10 to #12", "2024-02-10 to 2024-03-01", "4 evidence units",
            "bounded history window, not full repository history",
        ):
            assert fragment in summary
        return cov

    first = check()
    env.reopen_registry()  # restart: registry + ledger come back from SQLite
    assert check() == first

    # Completing the backfill flips the honest wording.
    vs.set_backfill_state(repo, backfill_cursor=None, history_complete=True)
    done = vs.compute_coverage(repo)
    assert done["history_complete"] is True and done["older_history_remaining"] is False
    assert done["selection"] == "full-closed-history"
    assert "bounded history window" not in done["summary"]


def test_coverage_marks_refresh_and_is_independent_of_job_success(env):
    repo = "acme/coverage-refresh"
    vs.index_prs(repo, [_pr(1, ["base doc"])])
    assert vs.compute_coverage(repo)["selection"] == "closed-created-window"
    vs.index_prs(repo, [_pr(2, ["refresh doc"])], is_refresh=True)
    vs.record_job_outcome(repo, status="failed", error_kind="rate_limited")
    cov = vs.compute_coverage(repo)
    assert cov["selection"] == "closed-created-window+refresh"
    assert cov["pull_requests_indexed"] == 2
    assert vs.get_index_registry(repo)["last_job_error_kind"] == "rate_limited"
    stats = vs.get_collection_stats(repo)
    assert (stats["last_job_status"], stats["last_job_error_kind"]) == ("failed", "rate_limited")


def test_coverage_for_an_unindexed_scope_is_honest(env):
    cov = vs.compute_coverage("acme/nothing-here")
    assert cov["pull_requests_indexed"] == 0 and cov["evidence_units"] == 0
    assert cov["generation"] == 0 and cov["pr_number_min"] is None
    assert cov["summary"].startswith("No closed pull requests")


# ── temporary registry ────────────────────────────────────────────────────────

def test_temporary_index_registry_is_not_persisted_across_restart(env):
    repo = f"acme/temp-{uuid.uuid4().hex[:8]}"
    try:
        vs.index_prs(repo, [_pr(1, ["temp doc"])], temporary=True)
        assert vs.repo_is_indexed_temporarily(repo)
        assert vs.get_active_generation(repo, storage="temporary") == 1
        assert vs.compute_coverage(repo, storage="temporary")["pull_requests_indexed"] == 1

        conn = sqlite3.connect(str(env.db_path))
        try:
            rows = conn.execute("SELECT COUNT(*) FROM index_cursors WHERE storage = 'temporary'").fetchone()[0]
            ledger = conn.execute("SELECT COUNT(*) FROM pr_ledger WHERE storage = 'temporary'").fetchone()[0]
        finally:
            conn.close()
        assert (rows, ledger) == (0, 0)

        restarted = env.reopen_registry()
        assert restarted.get_registry(repo, storage="temporary")["active_generation"] == 0
        assert restarted.get_cursor(repo, storage="temporary") == 0
        # Chroma's ephemeral data is gone after a real restart; with the registry
        # reset, no stale cursor survives to hide that history is missing.
        assert restarted.get_updated_at(repo, storage="temporary") is None
    finally:
        vs.delete_repo_index(repo, storage="temporary")


# ── delete + epoch ────────────────────────────────────────────────────────────

def test_delete_removes_every_generation_registry_and_ledger_rows(env):
    repo = "acme/delete-all"
    vs.index_prs(repo, [_pr(1, ["one"])])
    vs.index_prs(repo, [_pr(2, ["two"])])
    vs.set_backfill_state(repo, backfill_cursor="x", history_complete=False)
    assert len(_scope_collections(repo)) == 2

    result = vs.delete_repo_index(repo, storage="permanent")
    assert result["deleted"]["permanent"] is True
    assert _scope_collections(repo) == []
    registry = cursor_module.cursor_store.get_registry(repo)
    assert registry["exists"] is False and registry["active_generation"] == 0
    conn = sqlite3.connect(str(env.db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM pr_ledger WHERE repo_key = ?", (repo,)).fetchone()[0] == 0
    finally:
        conn.close()
    assert not vs.repo_is_indexed_permanently(repo)
    assert vs.compute_coverage(repo)["pull_requests_indexed"] == 0


def test_delete_also_removes_a_legacy_collection(env):
    repo = "acme/delete-legacy"
    _seed_legacy(repo)
    vs.delete_repo_index(repo)
    assert _scope_collections(repo) == []


def test_late_write_after_delete_cannot_resurrect_the_index(env):
    repo = "acme/epoch-late"
    epoch = vs.get_index_epoch(repo)
    vs.delete_repo_index(repo)
    with pytest.raises(vs.IndexInvalidatedError):
        vs.index_prs(repo, [_pr(1, ["body"])], expected_epoch=epoch)
    assert _scope_collections(repo) == []
    assert cursor_module.cursor_store.get_registry(repo)["exists"] is False


def test_delete_while_a_write_is_staged_cannot_publish(env, monkeypatch):
    repo = "acme/epoch-staged"
    vs.index_prs(repo, [_pr(1, ["existing"])])
    epoch = vs.get_index_epoch(repo)
    real_stale = vs._stale_document_ids

    def delete_mid_staging(*args, **kwargs):
        vs.delete_repo_index(repo)  # re-entrant lock: same thread, mid-stage
        return real_stale(*args, **kwargs)

    monkeypatch.setattr(vs, "_stale_document_ids", delete_mid_staging)
    # Whatever error surfaces (stale epoch or the staged collection vanishing),
    # nothing may be published or resurrected.
    with pytest.raises(Exception):
        vs.index_prs(repo, [_pr(2, ["late"])], expected_epoch=epoch)

    assert _scope_collections(repo) == []
    assert not vs.repo_is_indexed_permanently(repo)
    registry = cursor_module.cursor_store.get_registry(repo)
    assert registry["exists"] is False and registry["active_generation"] == 0
    assert cursor_module.cursor_store.get_ledger(repo) == []


def test_publish_rechecks_epoch_after_staging_succeeds(env, monkeypatch):
    repo = "acme/epoch-publish"
    vs.index_prs(repo, [_pr(1, ["existing"])])
    epoch = vs.get_index_epoch(repo)
    real_publish = cursor_module.cursor_store.publish_generation

    def bump_then_publish(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("publish must not run after the epoch changed")

    # A delete between validation and publish bumps the epoch; emulate it by
    # bumping right as validation completes (staged.count is the last read).
    original_assert = vs._assert_current_epoch
    calls = {"n": 0}

    def assert_with_bump(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the pre-publish re-check
            key = vs._index_scope_key(repo, False, None)
            vs._INDEX_EPOCHS[key] = vs._INDEX_EPOCHS.get(key, 0) + 1
        return original_assert(*args, **kwargs)

    monkeypatch.setattr(vs, "_assert_current_epoch", assert_with_bump)
    monkeypatch.setattr(cursor_module.cursor_store, "publish_generation", bump_then_publish)
    with pytest.raises(vs.IndexInvalidatedError):
        vs.index_prs(repo, [_pr(2, ["late"])], expected_epoch=epoch)
    monkeypatch.setattr(cursor_module.cursor_store, "publish_generation", real_publish)
    assert _scope_collections(repo) == [vs._generation_name(repo, 1)]
    assert vs.get_active_generation(repo) == 1


# ── retrieval helpers ─────────────────────────────────────────────────────────

def test_query_similar_keeps_provenance_and_supports_filters(env):
    repo = "acme/query-provenance"
    vs.index_prs(
        repo,
        [
            _pr(1, [{"text": "human note about caching strategy", "source_kind": "human_review_thread"}]),
            _pr(2, [{"text": "bot note about caching strategy", "source_kind": "bot_review_thread", "is_bot": True,
                     "source_url": "https://example.test/bot"}]),
            _pr(3, [{"text": "desc: caching strategy rewrite", "source_kind": "pr_description", "type": "pr_description"}]),
        ],
    )
    results = vs.query_similar(repo, "caching strategy", n_results=5)
    assert {r["id"] for r in results} == {"pr1-d0", "pr2-d0", "pr3-d0"}
    bot = next(r for r in results if r["id"] == "pr2-d0")
    assert bot["metadata"]["is_bot"] is True
    assert bot["metadata"]["source_url"] == "https://example.test/bot"
    assert bot["metadata"]["author_type"] == "bot"
    assert "namespace" not in bot["metadata"]
    assert set(bot) >= {"id", "text", "metadata", "similarity"}

    no_bots = vs.query_similar(repo, "caching strategy", n_results=5, exclude_bots=True)
    assert {r["id"] for r in no_bots} == {"pr1-d0", "pr3-d0"}
    only_desc = vs.query_similar(repo, "caching strategy", n_results=5, source_kinds=["pr_description"])
    assert [r["id"] for r in only_desc] == ["pr3-d0"]
    # Filtering happens before the cut: n_results=1 still yields the best human unit.
    one = vs.query_similar(repo, "bot note about caching strategy", n_results=1, exclude_bots=True)
    assert len(one) == 1 and one[0]["metadata"]["is_bot"] is False
    filtered = vs.query_similar(repo, "caching", n_results=5, where={"pr_number": 3})
    assert [r["id"] for r in filtered] == ["pr3-d0"]


def test_get_documents_by_ids_and_thread_parts(env):
    repo = "acme/threads"
    parts = [
        {"text": f"part {i} of a long thread", "thread_evidence_id": "thr-1", "part_index": i, "part_count": 3}
        for i in (2, 0, 1)
    ]
    vs.index_prs(repo, [_pr(5, parts + [{"text": "standalone unit"}])])
    docs = vs.get_documents_by_ids(repo, ["pr5-d3", "missing", "pr5-d0"])
    assert [d["id"] for d in docs] == ["pr5-d3", "pr5-d0"]
    assert docs[0]["text"] == "standalone unit"

    by_thread = vs.get_thread_parts(repo, "thr-1")
    assert [p["metadata"]["part_index"] for p in by_thread] == [0, 1, 2]
    by_part = vs.get_thread_parts(repo, "pr5-d1")  # any part id resolves to its thread
    assert [p["id"] for p in by_part] == [p["id"] for p in by_thread]
    assert [p["id"] for p in vs.get_thread_parts(repo, "pr5-d3")] == ["pr5-d3"]
    assert vs.get_thread_parts(repo, "nope") == []
    assert vs.get_documents_by_ids("acme/never-indexed", ["x"]) == []


def test_lexical_candidates_finds_exact_identifiers_paths_and_pr_refs(env):
    repo = "acme/lexical"
    vs.index_prs(
        repo,
        [
            _pr(1, [
                {"text": "We should never call parse_widget_config before the cache is warm.",
                 "path": "src/widgets/config.py"},
                {"text": "general discussion about widgets and warmth"},
            ]),
            _pr(2, [
                {"text": "Bot says: consider caching widgets", "is_bot": True, "source_kind": "bot_review_thread"},
                {"text": "unrelated text about documentation tone"},
            ]),
        ],
    )
    hits = vs.lexical_candidates(repo, "why does parse_widget_config break?", limit=5)
    assert hits and hits[0]["id"] == "pr1-d0"
    assert hits[0]["match"] == "exact"
    assert hits[0]["matched_identifiers"] == ["parse_widget_config"]

    by_path = vs.lexical_candidates(repo, "history of src/widgets/config.py", limit=5)
    assert by_path[0]["id"] == "pr1-d0" and by_path[0]["match"] == "exact"

    by_pr = vs.lexical_candidates(repo, "what happened in PR #2", limit=5)
    assert by_pr and by_pr[0]["metadata"]["pr_number"] == 2 and by_pr[0]["match"] == "exact"

    token_only = vs.lexical_candidates(repo, "documentation tone", limit=5)
    assert token_only[0]["id"] == "pr2-d1" and token_only[0]["match"] == "lexical"

    assert vs.lexical_candidates(repo, "zzz qqq nothing", limit=5) == []
    no_bots = vs.lexical_candidates(repo, "caching widgets", limit=5, exclude_bots=True)
    assert all(not h["metadata"]["is_bot"] for h in no_bots)
    assert vs.lexical_candidates("acme/never-indexed", "parse_widget_config") == []


def test_extract_query_identifiers_recognises_code_like_tokens():
    parsed = vs.extract_query_identifiers(
        "Did `retry_policy` or CursorStore in storage/cursor_store.py change in PR #123? e.g. now"
    )
    lowered = {i.lower() for i in parsed["identifiers"]}
    assert {"retry_policy", "cursorstore", "storage/cursor_store.py"} <= lowered
    assert "e.g" not in lowered
    assert parsed["pr_numbers"] == [123]


def test_list_all_repos_reports_one_row_per_scope_using_the_active_generation(env):
    repo = "acme/list-once"
    vs.index_prs(repo, [_pr(1, ["one"])])
    vs.index_prs(repo, [_pr(2, ["two"])])
    rows = [r for r in vs.list_all_repos() if r["repo"] == repo]
    assert len(rows) == 1
    assert (rows[0]["total_documents"], rows[0]["generation"]) == (2, 2)


def test_generation_names_fit_chroma_limit_for_long_repo_names(env):
    repo = "some-very-long-organisation-name/an-even-longer-repository-name-for-limits"
    assert len(vs._generation_name(repo, 999999)) <= 63
    vs.index_prs(repo, [_pr(1, ["body text"])])
    assert vs.get_active_generation(repo) == 1


# ── review fixes: coverage unknowns, legacy ledgers, reconcile, is_refresh ────

def test_unknown_history_completeness_counts_as_older_history_remaining(env):
    repo = "acme/unknown-history"
    vs.index_prs(repo, [_pr(1, ["doc one"])])
    cov = vs.compute_coverage(repo)
    assert cov["history_complete"] is None
    assert cov["older_history_remaining"] is True
    assert "history completeness is unknown" in cov["summary"]
    vs.set_backfill_state(repo, history_complete=True)
    cov = vs.compute_coverage(repo)
    assert cov["older_history_remaining"] is False
    assert "history completeness is unknown" not in cov["summary"]


def test_legacy_ledger_without_created_dates_reports_null_window(env):
    repo = "acme/legacy-nodates"
    col = vs._persistent_client.get_or_create_collection(
        name=vs._collection_name(repo), metadata=vs._collection_metadata(repo)
    )
    col.upsert(
        ids=["a", "b"],
        documents=["doc a", "doc b"],
        embeddings=[_vector("doc a"), _vector("doc b")],
        metadatas=[
            {"type": "review_comment", "pr_number": 3, "pr_created_at": "2022-05-01T00:00:00Z"},
            {"type": "review_comment", "pr_number": 4},  # no pr_created_at
        ],
    )
    cov = vs.compute_coverage(repo)  # derived straight from metadata
    assert cov["pull_requests_indexed"] == 2
    assert cov["oldest_pr_created_at"] is None and cov["newest_pr_created_at"] is None
    # After the first new write the ledger carries rows derived from legacy docs:
    vs.index_prs(repo, [_pr(5, ["new doc"], created="2024-01-01T00:00:00Z")])
    cov = vs.compute_coverage(repo)
    assert cov["pull_requests_indexed"] == 3
    assert cov["oldest_pr_created_at"] is None and cov["newest_pr_created_at"] is None
    assert "created " not in cov["summary"]


def test_nested_incomplete_units_count_distinct_threads_not_parts(env):
    repo = "acme/nested-distinct"
    col = vs._persistent_client.get_or_create_collection(
        name=vs._collection_name(repo), metadata=vs._collection_metadata(repo)
    )
    base = {"type": "review_thread", "pr_number": 9, "pr_created_at": "2023-01-01T00:00:00Z",
            "thread_incomplete": True}
    metas = [
        {**base, "thread_evidence_id": "T1:aaa", "evidence_id": "T1:p0", "part_index": 0, "part_count": 3},
        {**base, "thread_evidence_id": "T1:aaa", "evidence_id": "T1:p1", "part_index": 1, "part_count": 3},
        {**base, "thread_evidence_id": "T1:aaa", "evidence_id": "T1:p2", "part_index": 2, "part_count": 3},
        {**base, "thread_evidence_id": "", "evidence_id": "T2:bbb"},
    ]
    col.upsert(ids=[m["evidence_id"] for m in metas], documents=["x"] * 4,
               embeddings=[_vector(str(i)) for i in range(4)], metadatas=metas)
    assert vs.compute_coverage(repo)["nested_incomplete_units"] == 2


def test_prs_with_truncated_review_threads_count_as_nested_incomplete(env):
    repo = "acme/threads-truncated"
    pr = _pr(1, ["only returned thread"])
    pr["truncated_connections"] = ["reviewThreads"]
    pr["nested_incomplete_threads"] = 0
    vs.index_prs(repo, [pr])
    assert vs.compute_coverage(repo)["nested_incomplete_units"] == 1
    # Derived path (legacy docs whose metadata records the truncation) agrees.
    legacy = "acme/threads-truncated-legacy"
    col = vs._persistent_client.get_or_create_collection(
        name=vs._collection_name(legacy), metadata=vs._collection_metadata(legacy)
    )
    col.upsert(ids=["l1"], documents=["x"], embeddings=[_vector("x")], metadatas=[{
        "type": "review_thread", "pr_number": 2, "pr_created_at": "2023-01-01T00:00:00Z",
        "truncated_connections": json.dumps(["reviewThreads"]),
    }])
    assert vs.compute_coverage(legacy)["nested_incomplete_units"] == 1


def _pr_ids(number, documents, **extra):
    """Like _pr but keeps each document's explicit ``id``."""
    pr = _pr(number, [])
    pr["documents"] = documents
    pr.update(extra)
    return pr


def _thread_doc(key, digest, text, **extra):
    return {"id": f"{key}:{digest}", "type": "review_thread", "text": text, **extra}


def _ids_in(repo):
    col = vs._get_collection(repo)
    return set(col.get(include=[])["ids"])


def test_reindex_replaces_prior_units_of_the_pr_wholesale(env):
    repo = "acme/reconcile-wholesale"
    vs.index_prs(repo, [_pr_ids(1, [
        {"id": "desc", "type": "pr_description", "text": "old description"},
        _thread_doc("T1", "v1", "thread one first version"),
        _thread_doc("T2", "v1", "thread two"),
        {"id": "N1:c", "type": "commit_message", "text": "old commit"},
        {"id": "rev1:r", "type": "review_summary", "text": "old summary"},
    ]), _pr_ids(2, [{"id": "other-pr", "type": "pr_description", "text": "untouched"}])])
    vs.index_prs(repo, [_pr_ids(1, [
        {"id": "desc2", "type": "pr_description", "text": "new description"},
        _thread_doc("T1", "v2", "thread one edited version"),
    ])])
    # T1 has exactly one version; everything else of PR 1 not rebuilt is gone; PR 2 untouched.
    assert _ids_in(repo) == {"desc2", "T1:v2", "other-pr"}


def test_reindex_with_truncated_review_threads_keeps_unreturned_threads_only(env):
    repo = "acme/reconcile-truncated"
    vs.index_prs(repo, [_pr_ids(1, [
        {"id": "desc", "type": "pr_description", "text": "description"},
        _thread_doc("T1", "v1", "thread one first version"),
        _thread_doc("T2", "v1", "thread two (not re-returned)"),
        {"id": "N1:c", "type": "commit_message", "text": "commit"},
    ])])
    partial = _pr_ids(1, [
        {"id": "desc", "type": "pr_description", "text": "description"},
        _thread_doc("T1", "v2", "thread one edited version"),
    ])
    partial["truncated_connections"] = ["reviewThreads"]
    vs.index_prs(repo, [partial])
    # T2 kept (GitHub did not return it), T1's old version replaced, commit
    # (connection complete) reconciled away.
    assert _ids_in(repo) == {"desc", "T1:v2", "T2:v1"}


def test_is_refresh_is_explicit_and_never_inferred(env):
    repo = "acme/refresh-flag"
    # Initial import written in several chunks must not read as a refresh.
    vs.index_prs(repo, [_pr(1, ["chunk one"])])
    vs.index_prs(repo, [_pr(2, ["chunk two"])])
    vs.index_prs(repo, [_pr(3, ["chunk three"])], advance_watermark=True)
    assert vs.compute_coverage(repo)["selection"] == "closed-created-window"
    assert vs.get_index_registry(repo)["refresh_applied"] is False
    vs.index_prs(repo, [_pr(4, ["real refresh"])], is_refresh=True)
    assert vs.compute_coverage(repo)["selection"] == "closed-created-window+refresh"
    # Explicit False/None never clears an already-applied refresh.
    vs.index_prs(repo, [_pr(5, ["later chunk"])], is_refresh=False)
    assert vs.compute_coverage(repo)["selection"] == "closed-created-window+refresh"


def test_legacy_upgrade_is_not_labelled_refresh(env):
    repo = "acme/legacy-upgrade-flag"
    _seed_legacy(repo)
    vs.index_prs(repo, [_pr(8, ["first write over a legacy index"])])
    assert vs.compute_coverage(repo)["selection"] == "closed-created-window"


def test_same_commit_in_two_prs_indexed_in_separate_writes_keeps_both_units(env, monkeypatch):
    from storage import document_builder

    monkeypatch.setattr(vs, "build_documents", document_builder.build_documents)

    def pr(number):
        return {
            "number": number, "title": f"PR {number}", "body": "", "state": "MERGED",
            "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z",
            "url": f"https://github.com/o/r/pull/{number}", "github_node_id": f"PR_{number}",
            "commits": [{"oid": "abc123", "message": "fix: shared commit across two PRs"}],
        }

    repo = "acme/shared-commit"
    vs.index_prs(repo, [pr(1)])
    vs.index_prs(repo, [pr(2)])
    metas = vs._get_collection(repo).get(include=["metadatas"])["metadatas"]
    assert sorted(m["pr_number"] for m in metas) == [1, 2]
    assert {m["pr_url"] for m in metas} == {"https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"}
    # Ids are the same as when both PRs are built in one batch.
    together = document_builder.build_documents([pr(1), pr(2)])[2]
    assert _ids_in(repo) == set(together)
    # Re-indexing PR 1 alone leaves PR 2's unit untouched.
    vs.index_prs(repo, [pr(1)])
    assert _ids_in(repo) == set(together)
