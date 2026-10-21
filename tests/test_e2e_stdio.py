"""End-to-end: the real server over stdio, driven by the official ``mcp`` client.

Each test starts ``tests/support/stub_server.py`` as a subprocess. It runs the
production entrypoint with GitHub replaced by recorded JSON fixtures and the
embedding model replaced by an offline hashing model, so nothing here needs a
network connection or a GitHub credential. Restarting means starting a second
subprocess against the same on-disk index.
"""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("chromadb", reason="ChromaDB is required for the end-to-end flow")
pytest.importorskip("mcp", reason="the mcp client is required for the end-to-end flow")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import github_replay  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STUB_SERVER = ROOT / "tests" / "support" / "stub_server.py"
REPO = "fixture-owner/fixture-repo"
PR_URL = f"https://github.com/{REPO}/pull"


class Server:
    """Where one test's index lives and how to (re)start a server on it."""

    def __init__(self, tmp_path: Path):
        self.log = str(tmp_path / "github-requests.jsonl")
        # E2E_USE_INSTALLED=1 runs the server from the installed package (the
        # one `pip install .` put in site-packages) instead of this checkout.
        use_installed = os.environ.get("E2E_USE_INSTALLED") == "1"
        self.cwd = tmp_path if use_installed else ROOT
        self.env = {
            **({} if use_installed else {"PYTHONPATH": str(ROOT)}),
            "CHROMA_PERSIST_DIR": str(tmp_path / "chroma"),
            "CURSOR_DB_PATH": str(tmp_path / "cursors.db"),
            "GITHUB_REPLAY_LOG": self.log,
            "HF_HUB_OFFLINE": "1",
            "HOME": str(tmp_path),
        }

    @asynccontextmanager
    async def session(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(STUB_SERVER)],
            env=self.env,
            cwd=str(self.cwd),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    def github_requests(self) -> list:
        return github_replay.read_request_log(self.log)


async def call(session: ClientSession, tool: str, **arguments) -> str:
    result = await session.call_tool(tool, arguments)
    assert not result.isError, result.content
    return result.content[0].text


async def stats(session: ClientSession) -> dict:
    return json.loads(await call(session, "get_index_stats", repo=REPO))


async def index_and_wait(session: ClientSession, pages: int) -> dict:
    started = await call(
        session, "ensure_repo_ready", repo=REPO, storage="permanent", pages=pages
    )
    assert "Background indexing started" in started
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = await stats(session)
        job = current["index_job"]
        if job and job["status"] in {"ready", "failed", "partial", "cancelled"}:
            assert job["status"] == "ready", job
            return current
        await asyncio.sleep(0.2)
    raise AssertionError("indexing did not finish within 60 seconds")


async def search(session: ClientSession, query: str, **arguments) -> list:
    raw = await call(
        session, "semantic_search_reviews", query=query, repo=REPO, n_results=20, **arguments
    )
    return json.loads(raw)["results"]


def hit_for(results: list, pr_number: int, doc_type: str) -> dict:
    matches = [
        r for r in results
        if r["metadata"]["pr_number"] == pr_number and r["metadata"]["type"] == doc_type
    ]
    assert len(matches) == 1, [r["metadata"] for r in results]
    return matches[0]


def test_index_query_and_restart_over_stdio(tmp_path):
    server = Server(tmp_path)

    async def first_run():
        async with server.session() as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"ensure_repo_ready", "get_index_stats", "semantic_search_reviews"} <= tools

            before = await stats(session)
            assert before["total_documents"] == 0
            assert before["bounds"] == {"recorded": False}
            assert before["history_complete"] is None

            after = await index_and_wait(session, pages=2)
            results = await search(session, "parameterized queries SQL injection")
            humans_only = await search(
                session, "dependency upgrade requests", exclude_bots=True
            )
            everyone = await search(session, "dependency upgrade requests")
            return after, results, humans_only, everyone

    after, results, humans_only, everyone = asyncio.run(first_run())

    # Bounds: every page was fetched, so history is complete (for merged/closed PRs).
    assert after["bounds"] == {
        "recorded": True,
        "indexed_pr_count": 4,
        "oldest_pr_number": 2,
        "newest_pr_number": 7,
        "oldest_pr_updated_at": "2024-01-05T09:00:00Z",
        "newest_pr_updated_at": "2024-05-09T12:00:00Z",
        "configured_pr_limit": 60,
        "configured_pages": 2,
        "history_truncated": False,
    }
    assert after["history_complete"] is True
    assert after["index_job"]["status"] == "ready"
    assert "does NOT mean the whole repository history" in after["index_job"]["message"]
    assert [r["cursor"] for r in server.github_requests()] == [None, "CURSOR_PAGE_2"]

    # Evidence links resolve to the PR, the review, and the exact comment.
    comment_hit = hit_for(results, 3, "review_comment")
    assert comment_hit["links"] == {
        "pr": f"{PR_URL}/3",
        "review": f"{PR_URL}/3#pullrequestreview-3001",
        "comment": f"{PR_URL}/3#discussion_r001",
    }
    assert comment_hit["author_type"] == "human"
    assert hit_for(results, 3, "review_summary")["links"]["comment"] is None

    # Bot classification comes from GitHub's user type, not the login text.
    by_pr = {(r["metadata"]["pr_number"], r["metadata"]["type"]): r for r in everyone}
    assert by_pr[(6, "pr_description")]["author_type"] == "bot"     # login "dependabot"
    assert by_pr[(6, "review_summary")]["author_type"] == "bot"     # login "github-actions"
    assert by_pr[(2, "review_summary")]["author_type"] == "human"   # login "release-bot", type User
    assert not [r for r in humans_only if r["author_type"] == "bot"]
    assert any(r["metadata"]["pr_number"] == 7 for r in humans_only)
    assert len(humans_only) < len(everyone)

    # Restart: a brand-new server process must find the same index on disk.
    async def second_run():
        async with server.session() as session:
            again = await stats(session)
            reuse = await call(session, "ensure_repo_ready", repo=REPO, storage="permanent")
            return again, reuse, await search(session, "parameterized queries SQL injection")

    again, reuse, results_again = asyncio.run(second_run())
    assert again["total_documents"] == after["total_documents"] > 0
    assert again["bounds"] == after["bounds"]
    assert again["coverage"] == after["coverage"]
    assert "already indexed permanently" in reuse
    assert "Coverage: 4 PRs indexed (#2 to #7" in reuse
    assert hit_for(results_again, 3, "review_comment")["links"] == comment_hit["links"]
    assert len(server.github_requests()) == 2  # the restart did not re-fetch


def test_capped_import_reports_partial_history_and_keeps_it_after_restart(tmp_path):
    server = Server(tmp_path)

    async def capped_run():
        async with server.session() as session:
            return await index_and_wait(session, pages=1)

    capped = asyncio.run(capped_run())
    bounds = capped["bounds"]
    assert (bounds["indexed_pr_count"], bounds["oldest_pr_number"], bounds["newest_pr_number"]) == (2, 6, 7)
    assert (bounds["configured_pr_limit"], bounds["configured_pages"]) == (30, 1)
    assert bounds["history_truncated"] is True
    assert capped["history_complete"] is False
    assert capped["coverage"].startswith("PARTIAL HISTORY")
    assert "NOT imported" in capped["coverage"]
    # "ready" is still reported for the job, but it never claims full history.
    assert capped["index_job"]["status"] == "ready"
    assert "does NOT mean" in capped["index_job"]["message"]
    assert len(server.github_requests()) == 1

    async def restarted():
        async with server.session() as session:
            return await stats(session)

    assert asyncio.run(restarted())["bounds"] == bounds
