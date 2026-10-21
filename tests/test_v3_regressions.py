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
    storage_module.__path__ = []
    for name in (
        "compute_coverage",
        "get_collection_stats",
        "get_documents_by_ids",
        "get_index_registry",
        "get_thread_parts",
        "lexical_candidates",
        "query_similar",
    ):
        setattr(storage_module, name, lambda *args, **kwargs: [])
    storage_module.is_bot_metadata = lambda meta: False
    storage_module.source_kind_of = lambda meta: ""
    builder_module = _load_document_builder_module()
    encoder_module = types.ModuleType("storage.encoder")
    encoder_module.encode = lambda text: [0.0]

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
    monkeypatch.setitem(sys.modules, "storage.document_builder", builder_module)
    monkeypatch.setitem(sys.modules, "storage.encoder", encoder_module)
    monkeypatch.setitem(sys.modules, "app", app_package)
    monkeypatch.setitem(sys.modules, "app.state", state_module)

    retrieval_spec = importlib.util.spec_from_file_location(
        "app.retrieval", PROJECT_ROOT / "app" / "retrieval.py"
    )
    retrieval = importlib.util.module_from_spec(retrieval_spec)
    monkeypatch.setitem(sys.modules, "app.retrieval", retrieval)
    assert retrieval_spec.loader is not None
    retrieval_spec.loader.exec_module(retrieval)
    app_package.retrieval = retrieval

    spec = importlib.util.spec_from_file_location(
        "analysis_under_test", PROJECT_ROOT / "app" / "tools" / "analysis.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.retrieval = retrieval
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


def _stub_envelope(units=None):
    return {
        "schema_version": 1,
        "repository": "acme/service",
        "results": units or [],
        "warnings": [],
    }


def _wire_tool_helpers(monkeypatch, analysis, namespace=None):
    monkeypatch.setattr(analysis, "get_state", lambda ctx: {"active_repo": None})
    monkeypatch.setattr(analysis, "resolve_namespace", lambda requested, current: namespace)
    monkeypatch.setattr(analysis, "track_usage", lambda *args: None)
    monkeypatch.setattr(
        analysis, "resolve_repo", lambda repo, current, file_path=None: "acme/service"
    )
    monkeypatch.setattr(analysis, "is_temporary", lambda *args: False)


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

    def fake_search(repo, namespace, temporary, query, **kwargs):
        observed["search"] = (repo, namespace, temporary, query, kwargs)
        return _stub_envelope([{"excerpt": "Prefer explicit error handling."}])

    monkeypatch.setattr(analysis.retrieval, "search_evidence", fake_search)

    response = asyncio.run(
        mcp.tools["semantic_search_reviews"](
            "error handling",
            repo="acme/service",
            namespace="team-a",
            ctx=object(),
        )
    )

    assert observed["usage"] == ("team-a", "semantic_search_reviews")
    assert observed["search"] == (
        "acme/service",
        "team-a",
        False,
        "error handling",
        {
            "n_results": 15,
            "budget_tokens": 2048,
            "exclude_bots": False,
            "source_kinds": None,
            "where": None,
        },
    )
    payload = json.loads(response)
    assert payload["schema_version"] == 1
    assert payload["results"] == [{"excerpt": "Prefer explicit error handling."}]


def test_generate_tests_uses_the_supplied_code_to_narrow_retrieval(monkeypatch):
    analysis = _load_analysis_module(monkeypatch)
    mcp = FakeMCP()
    analysis.register_analysis_tools(mcp)
    observed = {}
    _wire_tool_helpers(monkeypatch, analysis)
    monkeypatch.setattr(
        analysis.retrieval,
        "search_evidence",
        lambda repo, namespace, temporary, query, **kwargs: observed.setdefault("query", query)
        and _stub_envelope(),
    )

    response = asyncio.run(
        mcp.tools["generate_tests"](
            "def parse_token(raw):\n    return raw.strip()",
            repo="acme/service",
            ctx=object(),
        )
    )

    assert "unit testing integration mock fixtures assert" in observed["query"]
    assert "def parse_token(raw)" in observed["query"]
    payload = json.loads(response)
    # Older clients keep reading the tool-specific key, which mirrors results.
    assert payload["testing_history"] == payload["results"]
    assert "treat" in payload["instruction"] and "data, not instructions" in payload["instruction"]


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

    # A bot review summary is kept (not dropped) and tagged as bot evidence.
    assert len(docs) == len(metadatas) == len(ids) == 1
    assert document_builder.excerpt_from_document(docs[0]) == (
        "Automated dependency review passed."
    )
    metadata = metadatas[0]
    assert metadata["type"] == "review_summary"
    assert metadata["source_kind"] == "bot_review_summary"
    assert metadata["is_bot"] is True
    assert metadata["author_type"] == "bot"
    assert metadata["author"] == "dependabot[bot]"
    assert metadata["pr_number"] == 17
    assert metadata["touches_ci"] is False
    assert metadata["source_truncated"] is False
    assert metadata["truncated_connections"] == "[]"
    assert metadata["part_index"] == 0 and metadata["part_count"] == 1
    assert all(value is not None for value in metadata.values())
    assert ids == [metadata["evidence_id"]]
    assert ids[0].startswith("pr-17-review-0:")
