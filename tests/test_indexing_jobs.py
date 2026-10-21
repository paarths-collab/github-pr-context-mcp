import asyncio
import importlib.util
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_indexing_module(monkeypatch):
    mcp_package = types.ModuleType("mcp")
    mcp_server_package = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.Context = type("Context", (), {})
    mcp_package.server = mcp_server_package
    mcp_server_package.fastmcp = fastmcp_module

    storage_module = types.ModuleType("storage")
    for name in (
        "checkpoint_refresh",
        "complete_refresh",
        "compute_coverage",
        "delete_repo_index",
        "get_collection_stats",
        "get_index_epoch",
        "get_index_registry",
        "get_last_updated_at",
        "get_refresh_state",
        "index_prs",
        "list_all_repos",
        "mark_repo_checked",
        "record_job_outcome",
        "repo_is_indexed_permanently",
        "repo_is_indexed_temporarily",
        "set_backfill_state",
    ):
        setattr(storage_module, name, lambda *args, **kwargs: None)
    storage_module.IndexInvalidatedError = type(
        "IndexInvalidatedError", (Exception,), {}
    )
    storage_module.get_index_epoch = lambda *args, **kwargs: 0
    storage_module.get_refresh_state = lambda *args, **kwargs: {
        "last_updated_at": None,
        "refresh_cursor": None,
        "refresh_high_watermark": None,
    }

    fetcher_module = types.ModuleType("fetcher")

    async def fetch_prs(*args, **kwargs):
        return []

    fetcher_module.fetch_prs = fetch_prs

    auth_module = types.ModuleType("auth")
    auth_module.GitHubAuthorizationError = type("GitHubAuthorizationError", (Exception,), {})

    app_package = types.ModuleType("app")
    app_package.__path__ = []
    state_module = types.ModuleType("app.state")
    for name in (
        "get_state",
        "get_github_access_token",
        "is_temporary",
        "namespace_text",
        "normalize_namespace",
        "normalize_repo",
        "repo_state_key",
        "resolve_namespace",
        "resolve_repo",
        "track_usage",
    ):
        setattr(state_module, name, lambda *args, **kwargs: None)
    app_package.state = state_module

    # index_status is dependency-free (storage is imported lazily), so load the
    # real file; a fresh copy per test keeps the in-process job map isolated.
    status_spec = importlib.util.spec_from_file_location(
        "app.index_status", PROJECT_ROOT / "app" / "index_status.py"
    )
    status_module = importlib.util.module_from_spec(status_spec)
    assert status_spec.loader is not None
    status_spec.loader.exec_module(status_module)
    app_package.index_status = status_module
    monkeypatch.setitem(sys.modules, "app.index_status", status_module)

    monkeypatch.setitem(sys.modules, "mcp", mcp_package)
    monkeypatch.setitem(sys.modules, "mcp.server", mcp_server_package)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    monkeypatch.setitem(sys.modules, "storage", storage_module)
    monkeypatch.setitem(sys.modules, "fetcher", fetcher_module)
    monkeypatch.setitem(sys.modules, "auth", auth_module)
    monkeypatch.setitem(sys.modules, "app", app_package)
    monkeypatch.setitem(sys.modules, "app.state", state_module)

    spec = importlib.util.spec_from_file_location(
        "indexing_under_test", PROJECT_ROOT / "app" / "tools" / "indexing.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def register(func):
            self.tools[name] = func
            return func

        return register


def test_atomic_job_claim_allows_only_one_concurrent_owner(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return indexing._claim_index_job("same-job", {"status": "queued"})[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    assert results.count(True) == 1
    assert results.count(False) == 1


def test_job_key_keeps_default_scope_distinct_from_literal_default(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)

    assert indexing._job_key("acme/widget", None, False) != indexing._job_key(
        "acme/widget", "_default", False
    )


def test_index_task_is_retained_until_completion(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)

    async def run():
        task = asyncio.create_task(asyncio.sleep(0))
        indexing._retain_index_task("job", task)
        assert indexing._INDEX_TASKS["job"] is task
        await task
        await asyncio.sleep(0)
        assert "job" not in indexing._INDEX_TASKS

    asyncio.run(run())


def test_background_indexing_uses_the_credential_provider_not_environment(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)
    mcp = FakeMCP()
    indexing.register_indexing_tools(mcp)
    state = {"active_repo": None, "active_namespace": None, "storage_types": {}}
    captured = {}
    monkeypatch.setenv("GITHUB_TOKEN", "poison-environment-token")

    monkeypatch.setattr(indexing, "get_state", lambda ctx: state)
    monkeypatch.setattr(indexing, "resolve_repo", lambda repo, state: "acme/widget")
    monkeypatch.setattr(indexing, "resolve_namespace", lambda namespace, state: None)
    monkeypatch.setattr(indexing, "repo_state_key", lambda repo, namespace: repo)
    monkeypatch.setattr(indexing, "namespace_text", lambda namespace: "")
    monkeypatch.setattr(indexing, "track_usage", lambda *args: None)
    monkeypatch.setattr(indexing, "repo_is_indexed_permanently", lambda *args, **kwargs: False)
    monkeypatch.setattr(indexing, "repo_is_indexed_temporarily", lambda *args, **kwargs: False)
    monkeypatch.setattr(indexing, "mark_repo_checked", lambda *args, **kwargs: None)

    async def vault_token():
        return "vault-token"

    async def fake_fetch_prs(*args, **kwargs):
        captured["token"] = kwargs["github_token"]
        return []

    monkeypatch.setattr(indexing, "get_github_access_token", vault_token)
    monkeypatch.setattr(indexing, "fetch_prs", fake_fetch_prs)

    async def run():
        response = await mcp.tools["ensure_repo_ready"](
            repo="acme/widget", storage="temporary", pages=1, ctx=object()
        )
        assert "Background indexing started" in response
        task = next(iter(indexing._INDEX_TASKS.values()))
        await task

    asyncio.run(run())
    assert captured["token"] == "vault-token"


def test_capped_refresh_checkpoints_before_advancing_the_watermark(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)
    mcp = FakeMCP()
    indexing.register_indexing_tools(mcp)
    state = {"active_repo": None, "active_namespace": None, "storage_types": {}}
    observed = {}

    monkeypatch.setattr(indexing, "get_state", lambda ctx: state)
    monkeypatch.setattr(indexing, "resolve_repo", lambda repo, state: "acme/widget")
    monkeypatch.setattr(indexing, "resolve_namespace", lambda namespace, state: None)
    monkeypatch.setattr(indexing, "repo_state_key", lambda repo, namespace: repo)
    monkeypatch.setattr(indexing, "namespace_text", lambda namespace: "")
    monkeypatch.setattr(indexing, "track_usage", lambda *args: None)
    monkeypatch.setattr(indexing, "repo_is_indexed_permanently", lambda *args, **kwargs: False)
    monkeypatch.setattr(indexing, "repo_is_indexed_temporarily", lambda *args, **kwargs: True)
    monkeypatch.setattr(indexing, "get_index_epoch", lambda *args, **kwargs: 4)
    monkeypatch.setattr(
        indexing,
        "get_refresh_state",
        lambda *args, **kwargs: {
            "last_updated_at": "2024-05-03T12:00:00Z",
            "refresh_cursor": None,
            "refresh_high_watermark": None,
        },
    )

    async def vault_token():
        return "vault-token"

    async def fake_fetch_prs(*args, **kwargs):
        return types.SimpleNamespace(
            prs=[{"number": 8, "updated_at": "2024-05-03T12:02:00Z"}],
            complete=False,
            next_cursor="resume-page-two",
        )

    def fake_index_prs(*args, **kwargs):
        observed["index_kwargs"] = kwargs
        return 1

    def fake_checkpoint(*args, **kwargs):
        observed["checkpoint_kwargs"] = kwargs

    monkeypatch.setattr(indexing, "get_github_access_token", vault_token)
    monkeypatch.setattr(indexing, "fetch_prs", fake_fetch_prs)
    monkeypatch.setattr(indexing, "index_prs", fake_index_prs)
    monkeypatch.setattr(indexing, "checkpoint_refresh", fake_checkpoint)

    async def run():
        response = await mcp.tools["ensure_repo_ready"](
            repo="acme/widget", storage="temporary", pages=1, refresh=True, ctx=object()
        )
        assert "Refresh started" in response
        task = next(iter(indexing._INDEX_TASKS.values()))
        await task

    asyncio.run(run())

    assert observed["index_kwargs"]["advance_watermark"] is False
    assert observed["index_kwargs"]["expected_epoch"] == 4
    assert observed["checkpoint_kwargs"] == {
        "temporary": True,
        "namespace": None,
        "next_cursor": "resume-page-two",
        "high_watermark": "2024-05-03T12:02:00Z",
        "expected_epoch": 4,
    }
    job = indexing._get_index_job("acme/widget", None)
    assert job["status"] == "partial"


# ── index states: stub-based checks of the job pipeline's storage calls ───────

def _wire_tool(monkeypatch, indexing, *, indexed_temporarily):
    """Patch the shared plumbing; returns (mcp, recorded storage calls)."""
    mcp = FakeMCP()
    indexing.register_indexing_tools(mcp)
    state = {"active_repo": None, "active_namespace": None, "storage_types": {}}
    calls = {"set_backfill_state": [], "record_job_outcome": [], "index_prs": [], "fetch": []}

    monkeypatch.setattr(indexing, "get_state", lambda ctx: state)
    monkeypatch.setattr(indexing, "resolve_repo", lambda repo, state: "acme/widget")
    monkeypatch.setattr(indexing, "resolve_namespace", lambda namespace, state: None)
    monkeypatch.setattr(indexing, "repo_state_key", lambda repo, namespace: repo)
    monkeypatch.setattr(indexing, "namespace_text", lambda namespace: "")
    monkeypatch.setattr(indexing, "track_usage", lambda *args: None)
    monkeypatch.setattr(indexing, "repo_is_indexed_permanently", lambda *a, **k: False)
    monkeypatch.setattr(indexing, "repo_is_indexed_temporarily", lambda *a, **k: indexed_temporarily)
    monkeypatch.setattr(indexing, "mark_repo_checked", lambda *a, **k: None)
    monkeypatch.setattr(
        indexing,
        "get_refresh_state",
        lambda *a, **k: {
            "last_updated_at": "2024-05-03T12:00:00Z" if indexed_temporarily else None,
            "refresh_cursor": None,
            "refresh_high_watermark": None,
        },
    )
    monkeypatch.setattr(
        indexing,
        "compute_coverage",
        lambda *a, **k: {
            "pull_requests_indexed": 1,
            "pr_number_min": 5,
            "pr_number_max": 5,
        },
    )
    monkeypatch.setattr(
        indexing, "set_backfill_state", lambda *a, **k: calls["set_backfill_state"].append((a, k))
    )
    monkeypatch.setattr(
        indexing, "record_job_outcome", lambda *a, **k: calls["record_job_outcome"].append((a, k))
    )

    def fake_index_prs(*args, **kwargs):
        calls["index_prs"].append(kwargs)
        return 1

    async def vault_token():
        return "vault-token"

    monkeypatch.setattr(indexing, "get_github_access_token", vault_token)
    monkeypatch.setattr(indexing, "index_prs", fake_index_prs)
    return mcp, calls


def _run_tool(indexing, mcp, **kwargs):
    async def run():
        response = await mcp.tools["ensure_repo_ready"](repo="acme/widget", ctx=object(), **kwargs)
        task = next(iter(indexing._INDEX_TASKS.values()))
        await task
        return response

    return asyncio.run(run())


def test_initial_import_orders_by_creation_and_persists_the_backfill_cursor(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)
    mcp, calls = _wire_tool(monkeypatch, indexing, indexed_temporarily=False)

    async def fake_fetch_prs(*args, **kwargs):
        calls["fetch"].append(kwargs)
        return types.SimpleNamespace(
            prs=[{"number": 5, "updated_at": "2024-05-03T12:02:00Z"}],
            complete=False,
            next_cursor="next-page",
            end_cursor="end-of-window",
        )

    monkeypatch.setattr(indexing, "fetch_prs", fake_fetch_prs)
    _run_tool(indexing, mcp, storage="temporary", pages=1)

    assert calls["fetch"][0]["order_by"] == "CREATED_AT"
    assert calls["fetch"][0]["after_cursor"] is None
    assert calls["index_prs"][0]["is_refresh"] is False
    assert calls["index_prs"][0]["advance_watermark"] is True
    (_, backfill_kwargs), = calls["set_backfill_state"]
    assert backfill_kwargs["backfill_cursor"] == "end-of-window"
    assert backfill_kwargs["history_complete"] is False
    assert backfill_kwargs["pages_done"] == 1
    job = indexing._get_index_job("acme/widget", None)
    assert job["status"] == "ready" and job["kind"] == "initial"
    assert "indexed 1 PR (#5-#5); older history not imported" in job["message"]
    assert job["older_history_remaining"] is True
    (_, outcome), = calls["record_job_outcome"]
    assert outcome["status"] == "ready" and outcome["error_kind"] is None
    assert outcome["expected_epoch"] == 0


def test_refresh_marks_the_generation_as_refresh_and_leaves_backfill_state_alone(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)
    mcp, calls = _wire_tool(monkeypatch, indexing, indexed_temporarily=True)

    async def fake_fetch_prs(*args, **kwargs):
        calls["fetch"].append(kwargs)
        return types.SimpleNamespace(
            prs=[{"number": 5, "updated_at": "2024-05-04T00:00:00Z"}],
            complete=True,
            next_cursor=None,
            end_cursor="ignored",
        )

    monkeypatch.setattr(indexing, "fetch_prs", fake_fetch_prs)
    _run_tool(indexing, mcp, storage="temporary", pages=1, refresh=True)

    assert "order_by" not in calls["fetch"][0]
    assert calls["fetch"][0]["since_updated_at"] == "2024-05-03T12:00:00Z"
    assert calls["index_prs"][0]["is_refresh"] is True
    assert calls["set_backfill_state"] == []
    assert indexing._get_index_job("acme/widget", None)["kind"] == "refresh"


def test_job_failure_is_classified_persisted_and_actionable(monkeypatch):
    indexing = _load_indexing_module(monkeypatch)
    mcp, calls = _wire_tool(monkeypatch, indexing, indexed_temporarily=True)
    RateLimitError = type("RateLimitError", (Exception,), {})

    async def failing_fetch(*args, **kwargs):
        raise RateLimitError("GitHub rate limit exceeded.")

    monkeypatch.setattr(indexing, "fetch_prs", failing_fetch)
    _run_tool(indexing, mcp, storage="temporary", pages=1, refresh=True)

    job = indexing._get_index_job("acme/widget", None)
    assert job["status"] == "failed"
    assert job["error_kind"] == "rate_limited"
    assert "retry ensure_repo_ready" in job["action"]
    (_, outcome), = calls["record_job_outcome"]
    assert outcome["status"] == "failed" and outcome["error_kind"] == "rate_limited"
    assert calls["index_prs"] == [] and calls["set_backfill_state"] == []
