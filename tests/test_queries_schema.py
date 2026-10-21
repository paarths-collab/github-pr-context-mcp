"""Structural guards on the GraphQL query strings.

Every other test in the suite feeds `flatten_prs` a hand-written JSON dict, so
none of them can notice when PR_QUERY asks GitHub for a field that does not
exist on the type it is nested under. That is exactly how `diffHunk` sat on
`PullRequestReviewThread` — where GitHub does not define it — long enough to
break indexing against the live API entirely.

These tests are offline: they assert the *shape* of the query text. They cannot
prove the whole query validates, only that the specific placements we have
already gotten wrong stay right.
"""

import re

from fetcher.queries import PR_QUERY


def _block(query: str, field: str) -> str:
    """Return the brace-balanced body of the selection set named `field`."""
    match = re.search(rf"\b{re.escape(field)}\b\s*(\([^)]*\))?\s*\{{", query)
    assert match, f"'{field}' selection set not found in query"

    start = query.index("{", match.end() - 1)
    depth = 0
    for i in range(start, len(query)):
        if query[i] == "{":
            depth += 1
        elif query[i] == "}":
            depth -= 1
            if depth == 0:
                return query[start + 1 : i]
    raise AssertionError(f"unbalanced braces in '{field}' selection set")


def test_diff_hunk_is_selected_on_the_review_comment_not_the_thread():
    """GitHub defines diffHunk on PullRequestReviewComment only.

    Selecting it on PullRequestReviewThread makes GitHub reject the whole query
    with `undefinedField`, which fails every fetch before a single PR is
    indexed.
    """
    threads = _block(PR_QUERY, "reviewThreads")
    comments = _block(threads, "comments")

    assert "diffHunk" in comments, "diffHunk must be selected on the review comment"

    thread_only = threads.replace(comments, "")
    assert "diffHunk" not in thread_only, (
        "diffHunk is selected directly on reviewThreads; GitHub does not define "
        "it on PullRequestReviewThread and will reject the query"
    )


def test_review_thread_keeps_the_fields_transform_reads():
    """flatten_pr reads path/line/isResolved off the thread, so they must stay."""
    threads = _block(PR_QUERY, "reviewThreads")
    comments = _block(threads, "comments")
    thread_only = threads.replace(comments, "")

    for field in ("isResolved", "path", "line"):
        assert field in thread_only, f"'{field}' must be selected on the review thread"


# --- v2 contract additions -------------------------------------------------

from fetcher.queries import (  # noqa: E402
    PR_QUERY_CREATED_AT,
    PR_REVIEW_THREADS_QUERY,
    THREAD_COMMENTS_QUERY,
    build_pr_query,
)


def _thread_only(query: str) -> tuple[str, str]:
    threads = _block(query, "reviewThreads")
    comments = _block(threads, "comments")
    return threads.replace(comments, ""), comments


def test_thread_level_fields_are_on_the_thread_and_comment_fields_on_the_comment():
    thread_only, comments = _thread_only(PR_QUERY)

    for field in ("isOutdated", "originalLine", "isResolved"):
        assert field in thread_only, f"'{field}' belongs on PullRequestReviewThread"
        assert field not in comments

    for field in ("databaseId", "url", "updatedAt", "originalCommit", "diffHunk"):
        assert field in comments, f"'{field}' belongs on PullRequestReviewComment"
        assert field not in thread_only


def test_urls_and_author_typenames_are_requested_everywhere():
    comments = _thread_only(PR_QUERY)[1]
    reviews = _block(PR_QUERY, "reviews")
    commits = _block(PR_QUERY, "commits")

    assert "originalCommit { oid }" in " ".join(comments.split())
    assert "url" in reviews and "updatedAt" in reviews
    assert "url" in commits
    # every actor selection carries __typename so bots are identifiable
    assert PR_QUERY.count("author { __typename login }") == 3  # PR, review comment, review
    assert "author { login }" not in PR_QUERY
    top = PR_QUERY.split("reviewThreads", 1)[0]
    assert "url" in top.split("files", 1)[0]  # PullRequest.url


def test_nested_page_info_includes_end_cursors_and_totals():
    threads = _block(PR_QUERY, "reviewThreads")
    comments = _block(threads, "comments")
    assert "endCursor" in comments and "totalCount" in comments
    assert "endCursor" in threads.replace(comments, "")


def test_order_variants_differ_only_in_the_order_field():
    assert "orderBy: {field: UPDATED_AT, direction: DESC}" in " ".join(PR_QUERY.split())
    assert "orderBy: {field: CREATED_AT, direction: DESC}" in " ".join(PR_QUERY_CREATED_AT.split())
    assert PR_QUERY_CREATED_AT.replace("CREATED_AT, direction", "UPDATED_AT, direction") == PR_QUERY
    assert build_pr_query("CREATED_AT") == PR_QUERY_CREATED_AT
    try:
        build_pr_query("STARS")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown order must be rejected")


def test_follow_up_queries_target_the_right_node_types():
    assert "... on PullRequestReviewThread" in THREAD_COMMENTS_QUERY
    assert "comments(first: 50, after: $cursor)" in THREAD_COMMENTS_QUERY
    assert "diffHunk" in THREAD_COMMENTS_QUERY and "originalCommit" in THREAD_COMMENTS_QUERY
    assert "... on PullRequest " in PR_REVIEW_THREADS_QUERY
    assert "reviewThreads(first: 100, after: $cursor)" in PR_REVIEW_THREADS_QUERY
    thread_only, _ = _thread_only(PR_REVIEW_THREADS_QUERY)
    assert "isOutdated" in thread_only and "diffHunk" not in thread_only
    for q in (THREAD_COMMENTS_QUERY, PR_REVIEW_THREADS_QUERY):
        assert q.count("{") == q.count("}")
