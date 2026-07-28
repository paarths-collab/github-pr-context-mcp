import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_analysis_module(monkeypatch):
    """Load analysis.py with light stubs so this unit test needs no ML packages."""
    mcp_package = types.ModuleType("mcp")
    mcp_server_package = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.Context = type("Context", (), {})
    mcp_package.server = mcp_server_package
    mcp_server_package.fastmcp = fastmcp_module

    storage_module = types.ModuleType("storage")
    storage_module.query_similar = lambda *args, **kwargs: []
    storage_module.get_collection_stats = lambda *args, **kwargs: {}

    app_package = types.ModuleType("app")
    app_package.__path__ = []
    state_module = types.ModuleType("app.state")
    for helper_name in (
        "get_state",
        "is_temporary",
        "resolve_namespace",
        "resolve_repo",
        "track_usage",
    ):
        setattr(state_module, helper_name, lambda *args, **kwargs: None)
    app_package.state = state_module

    monkeypatch.setitem(sys.modules, "mcp", mcp_package)
    monkeypatch.setitem(sys.modules, "mcp.server", mcp_server_package)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    monkeypatch.setitem(sys.modules, "storage", storage_module)
    monkeypatch.setitem(sys.modules, "app", app_package)
    monkeypatch.setitem(sys.modules, "app.state", state_module)

    spec = importlib.util.spec_from_file_location(
        "analysis_under_test", PROJECT_ROOT / "app" / "tools" / "analysis.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_document_builder_module():
    spec = importlib.util.spec_from_file_location(
        "document_builder_under_test",
        PROJECT_ROOT / "storage" / "document_builder.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeMCP:
    """Minimal decorator registry for invoking a registered MCP tool in a unit test."""

    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def register(func):
            self.tools[name] = func
            return func

        return register


def test_semantic_search_reviews_resolves_state_helpers(monkeypatch):
    """Analysis tools must import and invoke the shared session-state helpers."""
    analysis = _load_analysis_module(monkeypatch)
    mcp = FakeMCP()
    analysis.register_analysis_tools(mcp)
    state = {"active_repo": None}
    observed = {}

    monkeypatch.setattr(analysis, "get_state", lambda ctx: state)
    monkeypatch.setattr(
        analysis,
        "resolve_namespace",
        lambda requested_namespace, current_state: "team-a",
    )
    monkeypatch.setattr(
        analysis,
        "track_usage",
        lambda ctx, namespace, tool_name: observed.update(
            usage=(namespace, tool_name)
        ),
    )
    monkeypatch.setattr(
        analysis,
        "resolve_repo",
        lambda repo, current_state, file_path=None: "acme/service",
    )
    monkeypatch.setattr(
        analysis,
        "is_temporary",
        lambda repo_key, namespace, current_state: False,
    )
    monkeypatch.setattr(
        analysis,
        "query_similar",
        lambda repo_key, query, **kwargs: observed.update(
            query=(repo_key, query, kwargs)
        )
        or [{"document": "Prefer explicit error handling."}],
    )
    monkeypatch.setattr(
        analysis,
        "get_collection_stats",
        lambda repo_key, **kwargs: {"is_stale": False},
    )

    response = asyncio.run(
        mcp.tools["semantic_search_reviews"](
            "error handling",
            repo="acme/service",
            namespace="team-a",
            ctx=object(),
        )
    )

    assert observed["usage"] == ("team-a", "semantic_search_reviews")
    assert observed["query"] == (
        "acme/service",
        "error handling",
        {
            "n_results": 15,
            "temporary": False,
            "namespace": "team-a",
            "where": None,
        },
    )
    assert json.loads(response) == {
        "results": [{"document": "Prefer explicit error handling."}]
    }


def test_generate_tests_uses_the_supplied_code_to_narrow_retrieval(monkeypatch):
    analysis = _load_analysis_module(monkeypatch)
    mcp = FakeMCP()
    analysis.register_analysis_tools(mcp)
    state = {"active_repo": None}
    observed = {}

    monkeypatch.setattr(analysis, "get_state", lambda ctx: state)
    monkeypatch.setattr(analysis, "resolve_namespace", lambda requested, current: None)
    monkeypatch.setattr(analysis, "track_usage", lambda *args: None)
    monkeypatch.setattr(
        analysis, "resolve_repo", lambda repo, current, file_path=None: "acme/service"
    )
    monkeypatch.setattr(analysis, "is_temporary", lambda *args: False)
    monkeypatch.setattr(
        analysis,
        "query_similar",
        lambda repo_key, query, **kwargs: observed.setdefault("query", query) or [],
    )
    monkeypatch.setattr(
        analysis, "get_collection_stats", lambda *args, **kwargs: {"is_stale": False}
    )

    asyncio.run(
        mcp.tools["generate_tests"](
            "def parse_token(raw):\n    return raw.strip()",
            repo="acme/service",
            ctx=object(),
        )
    )

    assert "unit testing integration mock fixtures assert" in observed["query"]
    assert "def parse_token(raw)" in observed["query"]


def test_review_summary_without_inline_comments_has_own_bot_metadata():
    """A review summary must not depend on a preceding inline review comment."""
    document_builder = _load_document_builder_module()
    prs = [
        {
            "number": 17,
            "title": "Update dependencies",
            "body": "",
            "author": "octocat",
            "files": [],
            "review_comments": [],
            "commits": [],
            "reviews": [
                {
                    "author": "dependabot[bot]",
                    "state": "APPROVED",
                    "body": "Automated dependency review passed.",
                }
            ],
        }
    ]

    docs, metadatas, ids = document_builder.build_documents(prs)

    assert docs == [
        "PR #17 overall review by dependabot[bot] [APPROVED]: "
        "Automated dependency review passed."
    ]
    assert metadatas[0] == {
        "type": "review_summary",
        "pr_number": 17,
        "state": "APPROVED",
        "author": "dependabot[bot]",
        "is_bot": True,
        "touches_ci": False,
        "source_truncated": False,
        "truncated_connections": "[]",
        "updated_at": "",
    }
    assert ids == ["pr-17-review-0"]
