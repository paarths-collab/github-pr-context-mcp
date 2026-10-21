"""Index states, bounded initial import, resumable backfill and job failures.

Drives the real ``ensure_repo_ready`` / ``get_index_stats`` / ``delete_repo_index``
tools against real storage (tmp Chroma + tmp SQLite registry) with a fake encoder,
a fake document builder and canned ``fetch_prs`` results.  No network, no model.
"""

import asyncio
import hashlib
import importlib
import json
import sqlite3
import types

import pytest

pytest.importorskip("chromadb", reason="ChromaDB is required for vector-store integration tests")

import chromadb  # noqa: E402

import storage  # noqa: E402
import storage.vector_store as vs  # noqa: E402
from app import index_status  # noqa: E402
from app.tools import indexing  # noqa: E402
from auth import GitHubAuthorizationError, GitHubAuthorizationRequired  # noqa: E402
from fetcher.client import FetchResult, RateLimitError  # noqa: E402
from storage.cursor_store import CursorStore  # noqa: E402

cursor_module = importlib.import_module("storage.cursor_store")


def _vector(text):
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(b + 1) / 256.0 for b in digest[:8]]


def _fake_build_documents(prs):
    docs, metas, ids = [], [], []
    for pr in prs:
        for i, text in enumerate(pr.get("texts", [f"discussion on pull request {pr['number']}"])):
            doc_id = f"pr{pr['number']}-d{i}"
            docs.append(text)
            ids.append(doc_id)
            metas.append({
                "type": "review_thread",
                "pr_number": pr["number"],
                "pr_created_at": pr.get("created_at", ""),
                "pr_updated_at": pr.get("updated_at", ""),
                "pr_state": pr.get("state", "MERGED"),
                "source_kind": "human_review_thread",
                "is_bot": False,
                "author_type": "human",
                "evidence_id": doc_id,
                "thread_evidence_id": "",
                "part_index": 0,
                "part_count": 1,
                "thread_incomplete": False,
            })
    return docs, metas, ids


def _pr(number, *, updated=None):
    created = f"2024-01-{number % 28 + 1:02d}T00:00:00Z"
    return {
        "number": number,
        "created_at": created,
        "updated_at": updated or created,
        "state": "MERGED",
        "nested_incomplete_threads": 0,
    }


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def register(func):
            self.tools[name] = func
            return func

        return register


class Env:
    """Wires the real indexing tools to tmp storage and a scripted fetcher."""

    repo = "acme/widget"

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        self.db_path = tmp_path / "cursors.db"
        self.fetch_calls: list[dict] = []
        self.script: list = []
        self.token_error: Exception | None = None
        self.state = {"active_repo": None, "active_namespace": None, "storage_types": {}}
        self.mcp = FakeMCP()
        self.restart()

        monkeypatch.setattr(vs, "_persistent_client", chromadb.PersistentClient(path=str(tmp_path / "chroma")))
        monkeypatch.setattr(vs, "encode", _vector)
        monkeypatch.setattr(vs, "encode_batch", lambda texts: [_vector(t) for t in texts])
        monkeypatch.setattr(vs, "build_documents", _fake_build_documents)

        monkeypatch.setattr(indexing, "get_state", lambda ctx: self.state)
        monkeypatch.setattr(indexing, "resolve_repo", lambda repo, state, file_path=None: repo or self.repo)
        monkeypatch.setattr(indexing, "resolve_namespace", lambda namespace, state: namespace)
        monkeypatch.setattr(indexing, "track_usage", lambda *args: None)
        monkeypatch.setattr(indexing, "is_temporary", lambda *args: False)
        monkeypatch.setattr(indexing, "get_github_access_token", self._token)
        monkeypatch.setattr(indexing, "fetch_prs", self._fetch)
        indexing._INDEX_JOBS.clear()
        indexing._INDEX_TASKS.clear()
        indexing.register_indexing_tools(self.mcp)

    # -- fakes ---------------------------------------------------------------
    async def _token(self):
        if self.token_error is not None:
            raise self.token_error
        return "test-token"

    async def _fetch(self, owner, repo, pages=2, github_token=None, **kwargs):
        self.fetch_calls.append({"pages": pages, "token": github_token, **kwargs})
        step = self.script.pop(0)
        if callable(step):
            step = step()
            if asyncio.iscoroutine(step):
                step = await step
        if isinstance(step, BaseException):
            raise step
        return step

    def restart(self):
        """Simulate a process restart: new registry connection, no in-process jobs."""
        store = CursorStore(str(self.db_path))
        self.monkeypatch.setattr(cursor_module, "cursor_store", store)
        self.monkeypatch.setattr(storage, "cursor_store", store)
        indexing._INDEX_JOBS.clear()
        indexing._INDEX_TASKS.clear()
        self.store = store

    # -- tool helpers --------------------------------------------------------
    async def ensure(self, wait=True, **kwargs):
        kwargs.setdefault("repo", self.repo)
        response = await self.mcp.tools["ensure_repo_ready"](ctx=object(), **kwargs)
        if wait:
            await self.drain()
        return response

    async def drain(self):
        tasks = list(indexing._INDEX_TASKS.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stats(self):
        return json.loads(await self.mcp.tools["get_index_stats"](ctx=object(), repo=self.repo))

    def registry(self):
        return vs.get_index_registry(self.repo, None, "permanent")


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path)


def run(coro):
    return asyncio.run(coro)


def _result(prs, *, complete, end_cursor=None):
    return FetchResult(
        prs=prs,
        complete=complete,
        next_cursor=None if complete else end_cursor,
        end_cursor=end_cursor,
    )


# ── initial capped import ─────────────────────────────────────────────────────

def test_initial_capped_import_is_bounded_selected_scope_with_backfill_cursor(env):
    env.script = [_result([_pr(12), _pr(11)], complete=False, end_cursor="cursor-2")]

    async def scenario():
        started = await env.ensure(storage="permanent", pages=1)
        assert "bounded window" in started
        return await env.stats()

    stats = run(scenario())

    call = env.fetch_calls[0]
    assert call["order_by"] == "CREATED_AT"
    assert call["after_cursor"] is None and call["since_updated_at"] is None

    job = stats["index_job"]
    assert job["status"] == "ready"
    assert job["kind"] == "initial"
    assert "indexed 2 PRs (#11-#12); older history not imported" in job["message"]
    assert "bounded window" in job["message"]
    assert stats["message"] == job["message"]

    assert stats["index_state"] == "ready_selected_scope"
    assert stats["stale_reason"] is None and stats["action"] is None
    assert stats["generation"] == 1 and stats["serving_generation"] == 1
    coverage = stats["coverage"]
    assert coverage["older_history_remaining"] is True
    assert coverage["history_complete"] is False
    assert coverage["pull_requests_indexed"] == 2
    assert (coverage["pr_number_min"], coverage["pr_number_max"]) == (11, 12)
    assert stats["last_job"]["status"] == "ready"

    registry = env.registry()
    assert registry["backfill_cursor"] == "cursor-2"
    assert registry["history_complete"] is False
    assert registry["backfill_pages_done"] == 1
    assert registry["last_job_status"] == "ready"


def test_complete_initial_import_is_full_history(env):
    env.script = [_result([_pr(3), _pr(2)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=2)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_state"] == "ready_selected_scope"
    assert stats["coverage"]["history_complete"] is True
    assert stats["coverage"]["older_history_remaining"] is False
    assert "full closed pull request history" in stats["index_job"]["message"]
    assert env.registry()["backfill_cursor"] is None


def test_already_indexed_short_circuit_carries_coverage_and_state(env):
    env.script = [_result([_pr(5)], complete=False, end_cursor="c")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        return await env.ensure(storage="permanent")

    response = run(scenario())
    assert "already indexed permanently" in response
    assert "Index state: ready_selected_scope" in response
    assert "Coverage:" in response and "bounded history window" in response
    assert len(env.fetch_calls) == 1  # the second call did not fetch


def test_refresh_and_backfill_together_are_rejected(env):
    async def scenario():
        with pytest.raises(ValueError, match="refresh and backfill"):
            await env.ensure(storage="permanent", refresh=True, backfill=True)

    run(scenario())


def test_backfill_without_an_index_explains_itself(env):
    async def scenario():
        return await env.ensure(storage="permanent", backfill=True)

    response = run(scenario())
    assert "not indexed" in response and "no history to continue" in response
    assert env.fetch_calls == []


# ── backfill ──────────────────────────────────────────────────────────────────

def test_backfill_after_restart_continues_from_persisted_cursor_and_completes(env):
    env.script = [_result([_pr(12), _pr(11)], complete=False, end_cursor="cursor-2")]

    async def initial():
        await env.ensure(storage="permanent", pages=1)

    run(initial())
    watermark_before = env.store.get_refresh_state(env.repo)["last_updated_at"]
    assert watermark_before

    env.restart()  # new process: registry reopened, in-process jobs gone
    older = _pr(9, updated="2030-01-01T00:00:00Z")  # updated long after the window
    env.script = [_result([_pr(10), older], complete=True, end_cursor="cursor-3")]

    async def resume():
        response = await env.ensure(backfill=True, pages=3)  # storage inherited
        assert "Backfill of older history started" in response
        return await env.stats()

    stats = run(resume())

    call = env.fetch_calls[1]
    assert call["after_cursor"] == "cursor-2"
    assert call["order_by"] == "CREATED_AT"
    assert call["pages"] == 3
    assert call["since_updated_at"] is None

    assert stats["index_job"]["kind"] == "backfill"
    assert stats["index_job"]["status"] == "ready"
    assert stats["index_state"] == "ready_selected_scope"
    coverage = stats["coverage"]
    assert coverage["history_complete"] is True
    assert coverage["older_history_remaining"] is False
    assert coverage["selection"] == "full-closed-history"
    assert coverage["pull_requests_indexed"] == 4
    assert (coverage["pr_number_min"], coverage["pr_number_max"]) == (9, 12)
    assert stats["generation"] == 2

    registry = env.registry()
    assert registry["backfill_cursor"] is None
    assert registry["history_complete"] is True
    # Backfill must not move the refresh watermark, even for a recently updated PR.
    assert env.store.get_refresh_state(env.repo)["last_updated_at"] == watermark_before

    async def again():
        return await env.ensure(backfill=True)

    response = run(again())
    assert response.startswith("Nothing to backfill")
    assert len(env.fetch_calls) == 2


def test_backfill_stopped_by_page_cap_is_ready_partial_then_resumes(env):
    env.script = [
        _result([_pr(20), _pr(19)], complete=False, end_cursor="c-a"),
        _result([_pr(18), _pr(17)], complete=False, end_cursor="c-b"),
        _result([_pr(16)], complete=True, end_cursor="c-c"),
    ]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        await env.ensure(backfill=True, pages=1)
        partial = await env.stats()
        await env.ensure(backfill=True, pages=1)
        return partial, await env.stats()

    partial, final = run(scenario())

    assert [c.get("after_cursor") for c in env.fetch_calls] == [None, "c-a", "c-b"]
    assert partial["index_job"]["status"] == "partial"
    assert partial["index_state"] == "ready_partial"
    assert "backfill=true" in partial["action"]
    assert partial["coverage"]["older_history_remaining"] is True
    assert partial["coverage"]["pull_requests_indexed"] == 4
    assert final["index_state"] == "ready_selected_scope"
    assert final["coverage"]["history_complete"] is True
    assert final["coverage"]["pull_requests_indexed"] == 5
    assert env.registry()["backfill_pages_done"] == 3


def test_failed_backfill_keeps_cursor_from_last_successful_publish(env):
    env.script = [
        _result([_pr(20), _pr(19)], complete=False, end_cursor="c-a"),
        RateLimitError(1_900_000_000),
        _result([_pr(18)], complete=True, end_cursor="c-b"),
    ]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        before = await env.stats()
        await env.ensure(backfill=True, pages=1)
        failed = await env.stats()
        await env.ensure(backfill=True, pages=1)
        return before, failed, await env.stats()

    before, failed, final = run(scenario())

    assert failed["index_job"]["status"] == "failed"
    assert failed["index_job"]["error_kind"] == "rate_limited"
    assert failed["index_state"] == "stale"
    assert "rate_limited" in failed["stale_reason"]
    assert failed["generation"] == before["generation"]
    assert failed["total_documents"] == before["total_documents"]
    # The retry resumed from the cursor of the last successful publish.
    assert env.fetch_calls[2]["after_cursor"] == "c-a"
    assert final["index_state"] == "ready_selected_scope"
    assert final["coverage"]["history_complete"] is True


# ── refresh failure ───────────────────────────────────────────────────────────

def test_refresh_failure_keeps_old_generation_and_becomes_stale(env):
    env.script = [
        _result([_pr(7), _pr(6)], complete=True, end_cursor="end"),
        ValueError("GitHub GraphQL errors: [boom]"),
    ]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        before = await env.stats()
        await env.ensure(refresh=True, pages=1)
        return before, await env.stats()

    before, after = run(scenario())

    refresh_call = env.fetch_calls[1]
    assert refresh_call["since_updated_at"]  # incremental, UPDATED_AT default
    assert "order_by" not in refresh_call

    assert after["index_state"] == "stale"
    assert after["stale_reason"]
    assert after["index_job"]["status"] == "failed"
    assert after["index_job"]["error_kind"] == "unknown"
    assert after["last_job"]["error_kind"] == "unknown"
    assert after["action"]
    assert after["generation"] == before["generation"] == 1
    assert after["serving_generation"] == 1
    assert after["total_documents"] == before["total_documents"]
    assert after["coverage"]["pull_requests_indexed"] == 2  # still served


def test_failure_state_survives_restart_and_recovers_after_a_good_refresh(env):
    env.script = [
        _result([_pr(7)], complete=True, end_cursor="end"),
        RateLimitError(1_900_000_000),
    ]

    async def fail():
        await env.ensure(storage="permanent", pages=1)
        await env.ensure(refresh=True, pages=1)

    run(fail())
    env.restart()

    described = index_status.describe_index_state(env.repo, None, "permanent")
    assert described["index_state"] == "stale"
    assert described["last_job"]["source"] == "registry"
    assert described["last_job"]["error_kind"] == "rate_limited"
    assert "rate limit" in described["action"].lower()

    env.script = [_result([], complete=True, end_cursor=None)]

    async def recover():
        await env.ensure(refresh=True, pages=1)

    run(recover())
    healed = index_status.describe_index_state(env.repo, None, "permanent")
    assert healed["index_state"] == "ready_selected_scope"
    assert healed["stale_reason"] is None


def test_capped_refresh_is_ready_partial_and_publishes_with_is_refresh(env):
    env.script = [
        _result([_pr(7)], complete=False, end_cursor="older"),
        _result([_pr(8, updated="2031-01-01T00:00:00Z")], complete=False, end_cursor="r2"),
    ]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        await env.ensure(refresh=True, pages=1)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_job"]["status"] == "partial"
    assert stats["index_state"] == "ready_partial"
    assert "refresh=true" in stats["action"]
    assert stats["coverage"]["selection"] == "closed-created-window+refresh"
    assert env.registry()["refresh_cursor"] == "r2"


# ── error classification & authorization ──────────────────────────────────────

def test_missing_authorization_is_never_an_empty_ready_answer(env):
    env.token_error = GitHubAuthorizationRequired("GitHub is not connected.")

    async def scenario():
        response = await env.ensure(storage="permanent", pages=1)
        return response, await env.stats()

    response, stats = run(scenario())
    assert "begin_github_authorization" in response and "complete_github_authorization" in response
    assert "error_kind: authorization_required" in response
    assert env.fetch_calls == []

    assert stats["index_state"] == "failed"
    assert stats["last_job"]["error_kind"] == "authorization_required"
    assert "begin_github_authorization" in stats["action"]
    assert stats["total_documents"] == 0
    assert env.registry()["last_job_error_kind"] == "authorization_required"


def test_auth_failure_during_refresh_marks_existing_index_stale(env):
    env.script = [_result([_pr(4)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        env.token_error = GitHubAuthorizationRequired("GitHub is not connected.")
        await env.ensure(refresh=True)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_state"] == "stale"
    assert stats["last_job"]["error_kind"] == "authorization_required"
    assert stats["total_documents"] == 1  # old generation still serves


def test_revoked_credential_during_fetch_is_authorization_revoked(env):
    env.script = [PermissionError("GitHub rejected the saved credential. Reconnect GitHub and try again.")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_state"] == "failed"
    assert stats["index_job"]["error_kind"] == "authorization_revoked"
    assert "disconnect_github" in stats["action"]
    assert stats["coverage"]["pull_requests_indexed"] == 0


def test_encoder_load_failure_is_embeddings_unavailable_and_creates_no_generation(env):
    from storage import encoder

    def broken_model():
        raise RuntimeError("model files are missing")

    env.monkeypatch.setattr(encoder, "_get_model", broken_model)
    env.monkeypatch.setattr(vs, "encode_batch", encoder.encode_batch)
    env.script = [_result([_pr(2)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_state"] == "failed"
    assert stats["index_job"]["error_kind"] == "embeddings_unavailable"
    assert stats["generation"] == 0 and stats["serving_generation"] is None
    assert vs.repo_is_indexed_permanently(env.repo) is False


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (GitHubAuthorizationRequired("not connected"), "authorization_required"),
        (GitHubAuthorizationError("The saved GitHub connection is invalid."), "authorization_revoked"),
        (PermissionError("GitHub rejected the saved credential."), "authorization_revoked"),
        (RateLimitError(1_900_000_000), "rate_limited"),
        (ValueError("Repository 'a/b' not found or inaccessible."), "repository_not_found"),
        (OSError("We couldn't connect to 'https://huggingface.co' to load all-MiniLM-L6-v2"), "embeddings_unavailable"),
        (sqlite3.OperationalError("database is locked"), "storage_error"),
        (RuntimeError("Generation 3 failed validation: expected 4 documents"), "unknown"),
    ],
)
def test_error_classification(exc, kind):
    assert index_status.classify_index_error(exc) == kind
    assert index_status.action_for(kind, exc)


def test_http_status_errors_are_classified_by_status_code():
    def status_error(code):
        error = RuntimeError(f"HTTP {code}")
        error.response = types.SimpleNamespace(status_code=code)
        return error

    assert index_status.classify_index_error(status_error(401)) == "authorization_revoked"
    assert index_status.classify_index_error(status_error(404)) == "repository_not_found"
    assert index_status.classify_index_error(status_error(403)) == "authorization_required"


def test_storage_failure_during_publish_is_storage_error(env):
    def broken_publish(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    env.monkeypatch.setattr(env.store, "publish_generation", broken_publish)
    env.script = [_result([_pr(2)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_state"] == "failed"
    assert stats["index_job"]["error_kind"] == "storage_error"
    assert vs.repo_is_indexed_permanently(env.repo) is False  # staged collection cleaned up


# ── delete during a running job / indexing state ──────────────────────────────

def test_delete_that_lands_during_fetch_cannot_resurrect_the_index(env):
    def delete_then_return():
        vs.delete_repo_index(env.repo, storage="permanent")  # epoch bump, as a delete would do
        return _result([_pr(9)], complete=False, end_cursor="c")

    env.script = [delete_then_return]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)

    run(scenario())

    job = indexing._get_index_job(env.repo, None)
    assert job["status"] == "cancelled"
    assert vs.repo_is_indexed_permanently(env.repo) is False
    registry = env.registry()
    assert registry["active_generation"] == 0
    assert registry["backfill_cursor"] is None
    assert registry["last_job_status"] is None  # the outcome write was refused too
    assert index_status.describe_index_state(env.repo, None, "permanent")["index_state"] == "no_index"


def test_delete_tool_cancels_a_running_job_and_nothing_is_written(env):
    async def scenario():
        gate = asyncio.Event()

        async def blocked_fetch():
            await gate.wait()
            return _result([_pr(9)], complete=True, end_cursor="c")

        env.script = [blocked_fetch]
        await env.ensure(wait=False, storage="permanent", pages=1)
        for _ in range(50):
            if env.fetch_calls:
                break
            await asyncio.sleep(0.01)
        assert env.fetch_calls
        message = await env.mcp.tools["delete_repo_index"](
            repo=env.repo, storage="both", ctx=object()
        )
        gate.set()
        await env.drain()
        return message

    run(scenario())
    job = indexing._get_index_job(env.repo, None)
    assert job["status"] == "cancelled"
    assert vs.repo_is_indexed_permanently(env.repo) is False
    assert env.registry()["active_generation"] == 0
    assert index_status.describe_index_state(env.repo, None, "permanent")["index_state"] == "no_index"


def test_indexing_state_while_a_job_runs_reports_the_serving_generation(env):
    env.script = [_result([_pr(7)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        gate = asyncio.Event()

        async def blocked_fetch():
            await gate.wait()
            return _result([], complete=True)

        env.script = [blocked_fetch]
        await env.ensure(wait=False, refresh=True, pages=1)
        for _ in range(50):
            if len(env.fetch_calls) == 2:
                break
            await asyncio.sleep(0.01)
        running = await env.stats()
        duplicate = await env.ensure(wait=False, refresh=True, pages=1)
        gate.set()
        await env.drain()
        return running, duplicate, await env.stats()

    running, duplicate, done = run(scenario())
    assert running["index_state"] == "indexing"
    assert running["serving_generation"] == 1
    assert running["index_job"]["status"] == "running"
    assert running["action"] is None
    assert "already running" in duplicate
    assert done["index_state"] == "ready_selected_scope"


def test_indexing_state_without_a_published_generation(env):
    async def scenario():
        gate = asyncio.Event()

        async def blocked_fetch():
            await gate.wait()
            return _result([_pr(3)], complete=True, end_cursor="end")

        env.script = [blocked_fetch]
        await env.ensure(wait=False, storage="permanent", pages=1)
        for _ in range(50):
            if env.fetch_calls:
                break
            await asyncio.sleep(0.01)
        running = await env.stats()
        gate.set()
        await env.drain()
        return running

    running = run(scenario())
    assert running["index_state"] == "indexing"
    assert running["serving_generation"] is None
    assert running["generation"] == 0


# ── describe_index_state ──────────────────────────────────────────────────────

def test_describe_index_state_shape_and_no_index(env):
    described = index_status.describe_index_state("acme/never-seen", None, "permanent")
    assert set(described) == {
        "index_state", "stale_reason", "coverage", "generation",
        "serving_generation", "last_job", "action",
    }
    assert described["index_state"] == "no_index"
    assert described["serving_generation"] is None
    assert described["last_job"] is None
    assert "ensure_repo_ready" in described["action"]
    assert described["coverage"]["pull_requests_indexed"] == 0
    assert described["index_state"] in index_status.INDEX_STATES


def test_index_checked_over_thirty_days_ago_is_stale(env):
    env.script = [_result([_pr(4)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)

    run(scenario())
    env.store.mark_checked(env.repo, checked_at="2020-01-01T00:00:00+00:00")
    indexing._INDEX_JOBS.clear()

    described = index_status.describe_index_state(env.repo, None, "permanent")
    assert described["index_state"] == "stale"
    assert "days ago" in described["stale_reason"]
    assert "refresh=true" in described["action"]
    assert described["serving_generation"] == 1
