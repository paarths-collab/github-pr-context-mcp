"""Evidence links and human/bot author types, from GraphQL node to retrieval hit."""

import pytest

from fetcher.queries import PR_QUERY
from fetcher.transform import _is_bot, flatten_pr
from storage.document_builder import build_documents

from test_queries_schema import _block

REPO_URL = "https://github.com/acme/widgets"


def _raw_pr(**overrides):
    node = {
        "id": "PR_1", "number": 5, "url": f"{REPO_URL}/pull/5", "title": "Fix parser",
        "body": "Fixes the parser crash on empty input.",
        "author": {"login": "alice", "__typename": "User"},
        "createdAt": "2024-01-01T00:00:00Z", "updatedAt": "2024-01-02T00:00:00Z",
        "mergedAt": "2024-01-02T00:00:00Z", "state": "MERGED", "additions": 1, "deletions": 1,
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "commits": {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"commit": {"oid": "abc", "message": "Fix parser crash"}}]},
        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
            "id": "T1", "isResolved": False, "path": "parser.py", "line": 3,
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{
                "id": "C1", "url": f"{REPO_URL}/pull/5#discussion_r11",
                "author": {"login": "bob", "__typename": "User"},
                "body": "Please handle the empty-string case explicitly here.",
                "createdAt": "2024-01-01T01:00:00Z", "diffHunk": "@@ -1 +1 @@",
                "pullRequestReview": {"url": f"{REPO_URL}/pull/5#pullrequestreview-9"},
            }]},
        }]},
        "reviews": {"pageInfo": {"hasNextPage": False}, "nodes": [{
            "id": "R1", "url": f"{REPO_URL}/pull/5#pullrequestreview-9",
            "author": {"login": "bob", "__typename": "User"}, "state": "COMMENTED",
            "body": "Overall this needs a regression test for the empty input.",
            "submittedAt": "2024-01-01T01:05:00Z",
        }]},
    }
    node.update(overrides)
    return node


# ── bot classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize("login,typename,expected", [
    ("dependabot", "Bot", True),             # GraphQL drops the "[bot]" suffix; the type is the signal
    ("some-new-app", "Bot", True),           # no name heuristic needed when the type says Bot
    ("alice", "User", False),
    ("release-bot", "User", False),          # a human whose login ends in "-bot"
    ("github-actions", "User", False),       # an explicit user type beats the name list
    ("renovate[bot]", None, True),           # REST-style login, no type available
    ("dependabot", None, True),              # legacy payload: known bot name
    ("alice", None, False),
    ("", None, False),
])
def test_bot_classification(login, typename, expected):
    assert _is_bot(login, typename) is expected


def test_flattened_authors_carry_a_human_or_bot_label():
    raw = _raw_pr(author={"login": "dependabot", "__typename": "Bot"})
    raw["reviews"]["nodes"].append({
        "id": "R2", "url": f"{REPO_URL}/pull/5#pullrequestreview-10",
        "author": {"login": "copilot-pull-request-reviewer", "__typename": "Bot"},
        "state": "COMMENTED", "body": "Copilot reviewed 1 file.", "submittedAt": "",
    })
    pr = flatten_pr(raw)
    assert pr["author_type"] == "bot" and pr["author_is_bot"] is True
    assert pr["review_comments"][0]["author_type"] == "human"
    assert [r["author_type"] for r in pr["reviews"]] == ["human", "bot"]


def test_deleted_account_is_ghost_and_human():
    pr = flatten_pr(_raw_pr(author=None))
    assert pr["author"] == "ghost" and pr["author_type"] == "human"


# ── links ─────────────────────────────────────────────────────────────────────

def test_flattened_records_keep_pr_review_and_comment_urls():
    pr = flatten_pr(_raw_pr())
    assert pr["url"] == f"{REPO_URL}/pull/5"
    assert pr["review_comments"][0]["url"] == f"{REPO_URL}/pull/5#discussion_r11"
    assert pr["review_comments"][0]["review_url"] == f"{REPO_URL}/pull/5#pullrequestreview-9"
    assert pr["reviews"][0]["url"] == f"{REPO_URL}/pull/5#pullrequestreview-9"


def test_non_https_urls_from_the_api_are_dropped():
    raw = _raw_pr(url="javascript:alert(1)")
    raw["reviews"]["nodes"][0]["url"] = "http://example.com/x"
    pr = flatten_pr(raw)
    assert pr["url"] == "" and pr["reviews"][0]["url"] == ""


def test_every_document_type_carries_links_and_author_type():
    _, metadatas, _ = build_documents([flatten_pr(_raw_pr())], repo_key="acme/widgets")
    by_type = {m["type"]: m for m in metadatas}
    assert set(by_type) == {"pr_description", "review_comment", "commit_message", "review_summary"}
    assert all(m["pr_url"] == f"{REPO_URL}/pull/5" and m["author_type"] == "human" for m in metadatas)
    assert by_type["review_comment"]["comment_url"].endswith("#discussion_r11")
    assert by_type["review_comment"]["review_url"].endswith("#pullrequestreview-9")
    assert by_type["review_summary"]["review_url"].endswith("#pullrequestreview-9")
    assert by_type["review_summary"]["comment_url"] == ""
    assert by_type["pr_description"]["review_url"] == by_type["pr_description"]["comment_url"] == ""


def test_pr_link_is_derived_when_a_stored_record_has_no_url():
    record = flatten_pr(_raw_pr())
    record.pop("url")
    _, metadatas, _ = build_documents([record], repo_key="acme/widgets")
    assert {m["pr_url"] for m in metadatas} == {f"{REPO_URL}/pull/5"}
    _, metadatas, _ = build_documents([record])  # no repo to derive from
    assert {m["pr_url"] for m in metadatas} == {""}


def test_commits_inherit_their_pr_authors_type():
    raw = _raw_pr(author={"login": "dependabot", "__typename": "Bot"})
    _, metadatas, _ = build_documents([flatten_pr(raw)], repo_key="acme/widgets")
    commit = next(m for m in metadatas if m["type"] == "commit_message")
    assert commit["author_type"] == "bot"


def test_query_requests_urls_and_actor_types_on_the_right_types():
    assert "url" in _block(PR_QUERY, "nodes").split("files")[0]  # the PR node itself
    assert "url" in _block(PR_QUERY, "reviews")
    comments = _block(PR_QUERY, "comments")
    assert "url" in comments and "pullRequestReview { url }" in comments
    for block in ("comments", "reviews"):
        assert "author { login __typename }" in _block(PR_QUERY, block)
    assert "author { login __typename }" in PR_QUERY.split("files(")[0]
