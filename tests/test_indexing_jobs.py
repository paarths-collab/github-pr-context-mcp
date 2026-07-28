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
        "delete_repo_index",
        "get_collection_stats",
        "get_index_epoch",
        "get_last_updated_at",
        "get_refresh_state",
        "index_prs",
        "list_all_repos",
        "mark_repo_checked",
        "repo_is_indexed_permanently",
        "repo_is_indexed_temporarily",
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
