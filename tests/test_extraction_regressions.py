"""Offline regression coverage for the v3 GitHub evidence extractor.

These tests deliberately mock the GitHub transport.  They describe the public
contract that keeps retrieval complete enough for the IDE agent to reason over
without pretending that truncated nested GraphQL connections are complete.
"""

import asyncio

import fetcher.client as client
from fetcher.queries import PR_QUERY
from fetcher.transform import flatten_pr
from storage.document_builder import build_documents


def _raw_pr(number: int, updated_at: str) -> dict:
    """Return the smallest complete PR node accepted by ``flatten_pr``."""
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "Context",
        "author": {"login": "developer"},
        "createdAt": "2024-05-01T10:00:00Z",
        "updatedAt": updated_at,
        "mergedAt": "2024-05-01T11:00:00Z",
        "additions": 3,
        "deletions": 1,
        "files": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "commits": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "reviews": {"nodes": [], "pageInfo": {"hasNextPage": False}},
    }


def _page(nodes: list[dict], *, has_next_page: bool, end_cursor: str | None) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                }
            }
        }
    }


def test_pr_query_uses_forward_newest_first_pagination():
    """The GraphQL connection must follow its newest-first ordering forwards."""
    normalized = " ".join(PR_QUERY.split())

    assert "first: 30" in normalized
    assert "after: $cursor" in normalized
    assert "last: 30" not in normalized
    assert "before: $cursor" not in normalized
    assert "hasNextPage" in normalized
    assert "endCursor" in normalized
    assert "orderBy: {field: UPDATED_AT, direction: DESC}" in normalized


def test_fetch_prs_uses_end_cursor_when_advancing_newest_first_pages(monkeypatch):
    """Page two must be fetched with page one's endCursor, not startCursor."""
    calls: list[dict] = []
    responses = [
        _page(
            [_raw_pr(30, "2024-05-03T12:00:00Z"), _raw_pr(29, "2024-05-03T11:00:00Z")],
            has_next_page=True,
            end_cursor="cursor-for-page-two",
        ),
        _page(
            [_raw_pr(28, "2024-05-03T10:00:00Z")],
            has_next_page=False,
            end_cursor=None,
        ),
    ]

    async def fake_run_query(query, variables, github_token=None):
        calls.append(variables.copy())
        return responses.pop(0)

    monkeypatch.setattr(client, "run_query", fake_run_query)

    prs = asyncio.run(
        client.fetch_prs("acme", "widget", pages=2, github_token="test-token")
    )

    assert [pr["number"] for pr in prs] == [30, 29, 28]
    assert calls[0]["owner"] == "acme"
    assert calls[0]["repo"] == "widget"
    assert calls[1]["cursor"] == "cursor-for-page-two"


def test_incremental_refresh_uses_updated_at_and_replays_the_watermark_boundary(
    monkeypatch,
):
    """A refresh cannot use PR number as its watermark.

    An old-numbered PR can be edited after indexing, and items at the exact
    stored timestamp must be replayed so timestamp precision cannot cause a
    missed update.  The old item proves that the fetcher stops once it has
    crossed the safe refresh boundary.
    """
    calls: list[dict] = []
    response = _page(
        [
            _raw_pr(11, "2024-05-03T12:01:00Z"),
            _raw_pr(7, "2024-05-03T12:00:00Z"),
            _raw_pr(500, "2024-05-03T11:30:00Z"),
        ],
        has_next_page=True,
        end_cursor="older-page-that-must-not-be-needed",
    )

    async def fake_run_query(query, variables, github_token=None):
        calls.append(variables.copy())
        return response

    monkeypatch.setattr(client, "run_query", fake_run_query)

    prs = asyncio.run(
        client.fetch_prs(
            "acme",
            "widget",
            pages=3,
            github_token="test-token",
            since_updated_at="2024-05-03T12:00:00Z",
        )
    )

    assert [pr["number"] for pr in prs] == [11, 7]
    assert len(calls) == 1
    assert prs[0]["updated_at"] == "2024-05-03T12:01:00Z"
    assert prs[1]["updated_at"] == "2024-05-03T12:00:00Z"


def test_fetch_prs_discloses_when_the_requested_history_page_limit_is_reached(monkeypatch):
    """A bounded initial index must not masquerade as complete PR history."""
    response = _page(
        [_raw_pr(12, "2024-05-03T12:01:00Z")],
        has_next_page=True,
        end_cursor="more-history",
    )

    async def fake_run_query(query, variables, github_token=None):
        return response

    monkeypatch.setattr(client, "run_query", fake_run_query)

    prs = asyncio.run(client.fetch_prs("acme", "widget", pages=1, github_token="test-token"))

    assert prs[0]["truncated_connections"] == ["pullRequests"]


def test_capped_incremental_refresh_returns_a_resumable_cursor_without_marking_prs_partial(monkeypatch):
    """A capped refresh must not pretend its watermark can safely advance."""
    calls: list[dict] = []
    response = _page(
        [_raw_pr(12, "2024-05-03T12:03:00Z")],
        has_next_page=True,
        end_cursor="resume-after-page-one",
    )

    async def fake_run_query(query, variables, github_token=None):
        calls.append(variables.copy())
        return response

    monkeypatch.setattr(client, "run_query", fake_run_query)

    result = asyncio.run(
        client.fetch_prs(
            "acme",
            "widget",
            pages=1,
            github_token="test-token",
            since_updated_at="2024-05-03T12:00:00Z",
            return_result=True,
        )
    )

    assert result.complete is False
    assert result.next_cursor == "resume-after-page-one"
    assert result.prs[0]["truncated_connections"] == []
    assert calls[0].get("cursor") is None


def test_resumed_incremental_refresh_starts_from_the_saved_cursor(monkeypatch):
    calls: list[dict] = []
    response = _page(
        [_raw_pr(7, "2024-05-03T11:54:00Z")],
        has_next_page=True,
        end_cursor="not-needed-after-watermark",
    )

    async def fake_run_query(query, variables, github_token=None):
        calls.append(variables.copy())
        return response

    monkeypatch.setattr(client, "run_query", fake_run_query)

    result = asyncio.run(
        client.fetch_prs(
            "acme",
            "widget",
            pages=1,
            github_token="test-token",
            since_updated_at="2024-05-03T12:00:00Z",
            after_cursor="saved-continuation-cursor",
            return_result=True,
        )
    )

    assert result.complete is True
    assert result.next_cursor is None
    assert result.prs == []
    assert calls[0]["cursor"] == "saved-continuation-cursor"


def test_flatten_pr_surfaces_every_truncated_nested_connection():
    """The agent must be told when GitHub returned only a partial PR record."""
    raw_pr = _raw_pr(42, "2024-05-03T12:00:00Z")
    raw_pr["files"]["pageInfo"] = {"hasNextPage": True}
    raw_pr["reviewThreads"] = {
        "nodes": [
            {
                "isResolved": False,
                "path": "src/service.py",
                "line": 42,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "reviewer"},
                            "body": "Please change this.",
                            "createdAt": "2024-05-03T12:00:00Z",
                            "diffHunk": "@@ -1 +1 @@",
                        }
                    ],
                    "pageInfo": {"hasNextPage": True},
                },
            }
        ],
        "pageInfo": {"hasNextPage": True},
    }
    raw_pr["commits"]["pageInfo"] = {"hasNextPage": True}
    raw_pr["reviews"]["pageInfo"] = {"hasNextPage": True}

    flattened = flatten_pr(raw_pr)

    assert set(flattened["truncated_connections"]) == {
        "files",
        "reviewThreads",
        "reviewThreads.comments",
        "commits",
        "reviews",
    }

    docs, metadatas, _ = build_documents([flattened])
    assert "[Extraction note:" in docs[0]
    assert metadatas[0]["source_truncated"] is True
    assert "reviewThreads.comments" in metadatas[0]["truncated_connections"]


def test_flatten_pr_tolerates_null_optional_connections_from_github():
    """A partial API response must not make indexing fail for the whole repo."""
    raw_pr = _raw_pr(43, "2024-05-03T12:00:00Z")
    raw_pr["files"] = None
    raw_pr["reviewThreads"] = None
    raw_pr["commits"] = None
    raw_pr["reviews"] = None

    flattened = flatten_pr(raw_pr)

    assert flattened["files"] == []
    assert flattened["review_comments"] == []
    assert flattened["commits"] == []
    assert flattened["reviews"] == []
