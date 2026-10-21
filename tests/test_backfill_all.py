"""``ensure_repo_ready(backfill_all=true)``: import all history as one background job.

Same harness as ``test_index_states``: the real tools against tmp Chroma + tmp
SQLite registry, a fake encoder/document builder and canned ``fetch_prs``
results.  No network, no model.
"""

import asyncio

import pytest

pytest.importorskip("chromadb", reason="ChromaDB is required for vector-store integration tests")

import storage.vector_store as vs  # noqa: E402
from app import index_status  # noqa: E402
from app.tools import indexing  # noqa: E402
from fetcher.client import RateLimitError  # noqa: E402
from test_index_states import Env, _pr, _result, run  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path)


async def _wait_for(predicate, *, tries=200):
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached in time")


def _three_chunks():
    return [
        _result([_pr(20), _pr(19)], complete=False, end_cursor="c1"),
        _result([_pr(18), _pr(17)], complete=False, end_cursor="c2"),
        _result([_pr(16)], complete=True, end_cursor="c3"),
    ]


# ── completes over several chunks ─────────────────────────────────────────────

def test_import_all_runs_initial_then_backfill_chunks_until_history_is_complete(env):
    env.script = _three_chunks()

    async def scenario():
        started = await env.ensure(storage="permanent", pages=1, backfill_all=True)
        return started, await env.stats()

    started, stats = run(scenario())

    assert "Import of all history started" in started
    assert len(env.fetch_calls) == 3  # ONE job, three chunks
    assert [c["after_cursor"] for c in env.fetch_calls] == [None, "c1", "c2"]
    assert [c["pages"] for c in env.fetch_calls] == [1, 1, 1]
    assert all(c["order_by"] == "CREATED_AT" and c["since_updated_at"] is None for c in env.fetch_calls)

    job = stats["index_job"]
    assert job["status"] == "ready"
    assert job["backfill_all"] is True
    assert job["history_complete"] is True and job["older_history_remaining"] is False
    assert (job["pages_done"], job["prs_indexed"], job["max_pages"]) == (3, 5, 50)
    assert job["action"] is None
    assert "full closed pull request history is imported" in job["message"]

    assert stats["index_state"] == "ready_selected_scope"
    assert stats["generation"] == 3  # one generation per chunk
    assert stats["coverage"]["history_complete"] is True
    assert stats["coverage"]["pull_requests_indexed"] == 5
    assert (stats["coverage"]["pr_number_min"], stats["coverage"]["pr_number_max"]) == (16, 20)
    registry = env.registry()
    assert registry["backfill_cursor"] is None and registry["history_complete"] is True
    assert registry["backfill_pages_done"] == 3
    assert registry["last_job_status"] == "ready"
    assert env.state["active_repo"] == env.repo


def test_import_all_on_an_existing_index_resumes_from_the_saved_cursor_without_initial(env):
    env.script = [_result([_pr(20), _pr(19)], complete=False, end_cursor="c1")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        watermark = env.store.get_refresh_state(env.repo)["last_updated_at"]
        env.script = [
            _result([_pr(18)], complete=False, end_cursor="c2"),
            _result([_pr(5, updated="2031-01-01T00:00:00Z")], complete=True, end_cursor="c3"),
        ]
        await env.ensure(pages=1, backfill_all=True)  # storage inherited
        return watermark, await env.stats()

    watermark, stats = run(scenario())

    assert [c["after_cursor"] for c in env.fetch_calls] == [None, "c1", "c2"]
    assert stats["index_job"]["kind"] == "backfill"
    assert stats["index_state"] == "ready_selected_scope"
    assert stats["coverage"]["history_complete"] is True
    # Only the initial window may move the refresh watermark.
    assert env.store.get_refresh_state(env.repo)["last_updated_at"] == watermark


def test_import_all_in_one_chunk_when_the_first_window_already_completes(env):
    env.script = [_result([_pr(3), _pr(2)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=2, backfill_all=True)
        return await env.stats()

    stats = run(scenario())
    assert len(env.fetch_calls) == 1
    assert stats["index_job"]["kind"] == "initial"
    assert stats["index_job"]["status"] == "ready"
    assert stats["index_job"]["pages_done"] == 1  # 2 PRs fit one page
    assert stats["coverage"]["history_complete"] is True


def test_import_all_of_an_empty_repository_is_ready_not_an_error(env):
    env.script = [_result([], complete=True, end_cursor=None)]

    async def scenario():
        await env.ensure(storage="permanent", pages=1, backfill_all=True)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_job"]["status"] == "ready"
    assert "No closed pull requests were found" in stats["index_job"]["message"]
    assert stats["index_job"]["history_complete"] is True


def test_import_all_on_a_complete_index_has_nothing_to_do(env):
    env.script = [_result([_pr(3)], complete=True, end_cursor="end")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        return await env.ensure(backfill_all=True)

    response = run(scenario())
    assert response.startswith("Nothing to backfill")
    assert len(env.fetch_calls) == 1


# ── the max_pages cap ─────────────────────────────────────────────────────────

def test_cap_hit_is_ready_partial_with_continue_action_and_is_resumable(env):
    env.script = _three_chunks()[:2]

    async def scenario():
        await env.ensure(storage="permanent", pages=1, backfill_all=True, max_pages=2)
        capped = await env.stats()
        env.script = [_result([_pr(16)], complete=True, end_cursor="c3")]
        await env.ensure(pages=1, backfill_all=True, max_pages=2)
        return capped, await env.stats()

    capped, final = run(scenario())

    assert len(env.fetch_calls) == 3  # 2 chunks (cap), then the resumed one
    job = capped["index_job"]
    assert job["status"] == "partial"
    assert job["history_complete"] is False and job["older_history_remaining"] is True
    assert (job["pages_done"], job["prs_indexed"], job["max_pages"]) == (2, 4, 2)
    assert "backfill_all=true" in job["message"] and "page cap" in job["message"]
    assert "backfill_all=true" in job["action"]
    assert capped["index_state"] == "ready_partial"
    assert "backfill_all=true" in capped["action"]
    assert capped["coverage"]["older_history_remaining"] is True
    assert capped["coverage"]["pull_requests_indexed"] == 4
    assert env.fetch_calls[2]["after_cursor"] == "c2"  # resumed from the published cursor

    assert final["index_state"] == "ready_selected_scope"
    assert final["index_job"]["status"] == "ready"
    assert final["coverage"]["history_complete"] is True
    assert final["coverage"]["pull_requests_indexed"] == 5


def test_chunk_size_is_trimmed_so_the_total_never_exceeds_max_pages(env):
    env.script = _three_chunks()[:2]

    async def scenario():
        await env.ensure(storage="permanent", pages=3, backfill_all=True, max_pages=4)
        return await env.stats()

    stats = run(scenario())
    assert [c["pages"] for c in env.fetch_calls] == [3, 1]
    assert stats["index_job"]["pages_done"] == 4
    assert stats["index_job"]["status"] == "partial"


@pytest.mark.parametrize(("requested", "effective"), [(0, 1), (-5, 1), (1, 1), (200, 200), (10_000, 200)])
def test_max_pages_is_hard_clamped_to_1_through_200(env, requested, effective):
    env.script = _three_chunks()[:1]

    async def scenario():
        await env.ensure(storage="permanent", pages=1, backfill_all=True, max_pages=requested)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_job"]["max_pages"] == effective
    assert stats["index_job"]["pages_done"] <= effective


def test_max_pages_one_still_publishes_the_initial_window(env):
    env.script = _three_chunks()[:1]

    async def scenario():
        await env.ensure(storage="permanent", pages=5, backfill_all=True, max_pages=1)
        return await env.stats()

    stats = run(scenario())
    assert [c["pages"] for c in env.fetch_calls] == [1]
    assert stats["index_state"] == "ready_partial"
    assert stats["generation"] == 1


# ── restart ───────────────────────────────────────────────────────────────────

def test_restart_mid_way_resumes_from_the_persisted_cursor(env):
    async def scenario():
        gate = asyncio.Event()

        async def hang():  # the process dies while chunk 3 is in flight
            await gate.wait()

        env.script = [
            _result([_pr(20), _pr(19)], complete=False, end_cursor="c1"),
            _result([_pr(18), _pr(17)], complete=False, end_cursor="c2"),
            hang,
        ]
        await env.ensure(wait=False, storage="permanent", pages=1, backfill_all=True)
        await _wait_for(lambda: len(env.fetch_calls) == 3)
        for task in list(indexing._INDEX_TASKS.values()):
            task.cancel()
        await env.drain()

    run(scenario())
    registry = env.registry()
    assert registry["backfill_cursor"] == "c2"  # last PUBLISHED chunk, not the in-flight one
    assert registry["active_generation"] == 2

    env.restart()  # new process: registry reopened, in-process jobs gone
    env.script = [_result([_pr(16)], complete=True, end_cursor="c3")]

    async def resume():
        await env.ensure(pages=1, backfill_all=True)
        return await env.stats()

    stats = run(resume())
    assert env.fetch_calls[3]["after_cursor"] == "c2"
    assert stats["index_job"]["status"] == "ready"
    assert stats["index_job"]["kind"] == "backfill"  # the initial window was not repeated
    assert stats["index_state"] == "ready_selected_scope"
    assert stats["coverage"]["history_complete"] is True
    assert stats["coverage"]["pull_requests_indexed"] == 5
    assert stats["generation"] == 3


# ── failures keep what was published ──────────────────────────────────────────

@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (RateLimitError(1_900_000_000), "rate_limited"),
        (PermissionError("GitHub rejected the saved credential."), "authorization_revoked"),
    ],
)
def test_failure_mid_way_keeps_published_chunks_and_is_resumable(env, error, kind):
    env.script = [
        _result([_pr(20), _pr(19)], complete=False, end_cursor="c1"),
        _result([_pr(18), _pr(17)], complete=False, end_cursor="c2"),
        error,
    ]

    async def scenario():
        await env.ensure(storage="permanent", pages=1, backfill_all=True)
        failed = await env.stats()
        registry = env.registry()
        env.script = [_result([_pr(16)], complete=True, end_cursor="c3")]
        await env.ensure(pages=1, backfill_all=True)
        return failed, registry, await env.stats()

    failed, registry, final = run(scenario())

    job = failed["index_job"]
    assert job["status"] == "failed"
    assert job["error_kind"] == kind and job["action"]
    assert (job["pages_done"], job["prs_indexed"]) == (2, 4)  # progress of the published chunks
    assert failed["index_state"] == "stale"
    assert kind in failed["stale_reason"]
    assert failed["action"]
    assert failed["generation"] == 2 and failed["serving_generation"] == 2
    assert failed["coverage"]["pull_requests_indexed"] == 4
    assert failed["total_documents"] == 4
    assert registry["backfill_cursor"] == "c2"
    assert registry["last_job_status"] == "failed" and registry["last_job_error_kind"] == kind
    # A failed backfill chunk is not a failed refresh.
    assert registry["last_refresh_status"] != "failed"

    assert env.fetch_calls[3]["after_cursor"] == "c2"
    assert final["index_state"] == "ready_selected_scope"
    assert final["coverage"]["history_complete"] is True


def test_failure_in_the_initial_chunk_publishes_nothing(env):
    env.script = [RateLimitError(1_900_000_000)]

    async def scenario():
        await env.ensure(storage="permanent", pages=1, backfill_all=True)
        return await env.stats()

    stats = run(scenario())
    assert stats["index_state"] == "failed"
    assert stats["index_job"]["error_kind"] == "rate_limited"
    assert stats["index_job"]["kind"] == "initial"
    assert stats["generation"] == 0
    assert vs.repo_is_indexed_permanently(env.repo) is False


def test_an_empty_incomplete_page_stops_the_import_instead_of_looping(env):
    env.script = [
        _result([_pr(20)], complete=False, end_cursor="c1"),
        _result([], complete=False, end_cursor="c1"),
    ]

    async def scenario():
        await env.ensure(storage="permanent", pages=1, backfill_all=True)
        return await env.stats()

    stats = run(scenario())
    assert len(env.fetch_calls) == 2
    assert stats["index_job"]["status"] == "failed"
    assert "stopping the import" in stats["index_job"]["error"]
    assert stats["generation"] == 1  # chunk 1 is kept
    assert env.registry()["backfill_cursor"] == "c1"


# ── delete while running ──────────────────────────────────────────────────────

def test_delete_tool_during_a_later_chunk_stops_the_loop_and_cannot_resurrect(env):
    async def scenario():
        gate = asyncio.Event()

        async def blocked():
            await gate.wait()
            return _result([_pr(18)], complete=False, end_cursor="c2")

        env.script = [
            _result([_pr(20), _pr(19)], complete=False, end_cursor="c1"),
            blocked,
            _result([_pr(17)], complete=True, end_cursor="c3"),  # must never be requested
        ]
        await env.ensure(wait=False, storage="permanent", pages=1, backfill_all=True)
        await _wait_for(lambda: len(env.fetch_calls) == 2)
        assert vs.repo_is_indexed_permanently(env.repo) is True  # chunk 1 was published
        await env.mcp.tools["delete_repo_index"](repo=env.repo, storage="both", ctx=object())
        gate.set()
        await env.drain()

    run(scenario())

    assert len(env.fetch_calls) == 2
    assert indexing._get_index_job(env.repo, None)["status"] == "cancelled"
    assert vs.repo_is_indexed_permanently(env.repo) is False
    registry = env.registry()
    assert registry["active_generation"] == 0
    assert registry["backfill_cursor"] is None
    assert registry["last_job_status"] is None
    assert index_status.describe_index_state(env.repo, None, "permanent")["index_state"] == "no_index"


def test_delete_that_lands_during_a_fetch_is_refused_by_the_epoch(env):
    def delete_then_return():
        vs.delete_repo_index(env.repo, storage="permanent")  # epoch bump, as a delete would do
        return _result([_pr(18), _pr(17)], complete=False, end_cursor="c2")

    env.script = [
        _result([_pr(20), _pr(19)], complete=False, end_cursor="c1"),
        delete_then_return,
        _result([_pr(16)], complete=True, end_cursor="c3"),  # must never be requested
    ]

    async def scenario():
        await env.ensure(storage="permanent", pages=1, backfill_all=True)

    run(scenario())

    assert len(env.fetch_calls) == 2
    assert indexing._get_index_job(env.repo, None)["status"] == "cancelled"
    assert vs.repo_is_indexed_permanently(env.repo) is False
    registry = env.registry()
    assert registry["active_generation"] == 0 and registry["backfill_cursor"] is None
    assert index_status.describe_index_state(env.repo, None, "permanent")["index_state"] == "no_index"


# ── progress while running ────────────────────────────────────────────────────

def test_progress_is_visible_in_get_index_stats_while_the_job_runs(env):
    async def scenario():
        gate = asyncio.Event()

        async def blocked():
            await gate.wait()
            return _result([_pr(16)], complete=True, end_cursor="c3")

        env.script = [
            _result([_pr(20), _pr(19)], complete=False, end_cursor="c1"),
            _result([_pr(18), _pr(17)], complete=False, end_cursor="c2"),
            blocked,
        ]
        await env.ensure(wait=False, storage="permanent", pages=1, backfill_all=True)
        await _wait_for(lambda: len(env.fetch_calls) == 3)
        await _wait_for(lambda: (indexing._get_index_job(env.repo, None) or {}).get("pages_done") == 2)
        running = await env.stats()
        duplicate = await env.ensure(wait=False, pages=1, backfill_all=True)
        gate.set()
        await env.drain()
        return running, duplicate, await env.stats()

    running, duplicate, done = run(scenario())

    job = running["index_job"]
    assert job["status"] == "running"
    assert (job["pages_done"], job["prs_indexed"], job["history_complete"]) == (2, 4, False)
    assert job["max_pages"] == 50
    assert running["index_state"] == "indexing"
    assert running["serving_generation"] == 2  # already searchable while the import continues
    assert running["generation"] == 2
    assert running["total_documents"] == 4
    assert "2/50 pages" in running["message"]
    assert running["last_job"]["pages_done"] == 2 and running["last_job"]["prs_indexed"] == 4
    assert "already running" in duplicate
    assert len(env.fetch_calls) == 3  # the duplicate did not start a second job

    assert done["index_state"] == "ready_selected_scope"
    assert done["index_job"]["pages_done"] == 3 and done["index_job"]["prs_indexed"] == 5
    assert done["index_job"]["history_complete"] is True


# ── invalid combinations ──────────────────────────────────────────────────────

@pytest.mark.parametrize("other", ["refresh", "backfill"])
def test_backfill_all_cannot_be_combined_with_refresh_or_backfill(env, other):
    async def scenario():
        with pytest.raises(ValueError, match="backfill_all cannot be combined with refresh or backfill"):
            await env.ensure(storage="permanent", backfill_all=True, **{other: True})

    run(scenario())
    assert env.fetch_calls == []
    assert indexing._get_index_job(env.repo, None) is None


def test_backfill_all_without_an_index_still_asks_for_a_storage_choice(env):
    async def scenario():
        return await env.ensure(backfill_all=True)

    response = run(scenario())
    assert "not indexed yet" in response and "permanent or temporary" in response
    assert "backfill_all=true" in response
    assert env.fetch_calls == []


def test_plain_calls_are_unchanged_by_the_new_parameters(env):
    env.script = [_result([_pr(12), _pr(11)], complete=False, end_cursor="c")]

    async def scenario():
        await env.ensure(storage="permanent", pages=1)
        return await env.stats()

    stats = run(scenario())
    job = stats["index_job"]
    assert job["backfill_all"] is False
    assert job["pages_done"] is None and job["prs_indexed"] is None and job["max_pages"] is None
    assert stats["index_state"] == "ready_selected_scope"
