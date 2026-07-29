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
