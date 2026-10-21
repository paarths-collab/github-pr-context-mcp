"""Offline coverage for nested pagination and CREATED_AT ordering in the fetcher."""

import asyncio

import fetcher.client as client


def _comment(n: int, *, bot: bool = False) -> dict:
    return {
        "id": f"C{n}",
        "databaseId": n,
        "url": f"https://github.com/acme/widget/pull/1#discussion_r{n}",
        "author": {"__typename": "Bot" if bot else "User", "login": "rev-bot" if bot else "rev"},
        "body": f"comment {n}",
        "createdAt": f"2024-05-01T10:{n % 60:02d}:00Z",
        "updatedAt": f"2024-05-01T10:{n % 60:02d}:00Z",
        "diffHunk": "@@ -1 +1 @@" if n == 1 else "",
        "originalCommit": {"oid": "deadbeef"},
    }


def _conn(nodes, *, has_next, end=None, total=None):
    conn = {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": end}}
    if total is not None:
        conn["totalCount"] = total
    return conn


def _thread(tid: str, comments_conn: dict) -> dict:
    return {
        "id": tid, "isResolved": False, "isOutdated": False,
        "path": "src/mod.py", "line": 3, "originalLine": 3,
        "comments": comments_conn,
    }


def _pr(number: int, *, threads_conn: dict, created="2024-05-01T10:00:00Z",
        updated="2024-05-02T10:00:00Z", files=None, commits=None, reviews=None) -> dict:
    return {
        "id": f"PR_{number}", "number": number,
        "url": f"https://github.com/acme/widget/pull/{number}",
        "title": f"PR {number}", "body": "b", "author": {"__typename": "User", "login": "dev"},
        "createdAt": created, "updatedAt": updated, "mergedAt": created,
        "additions": 1, "deletions": 1,
        "files": files or _conn([], has_next=False), "reviewThreads": threads_conn,
        "commits": commits or _conn([], has_next=False),
        "reviews": reviews or _conn([], has_next=False),
    }


def _page(nodes, *, has_next, end):
    return {"data": {"repository": {"pullRequests": {
        "nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": end}}}}}


def _node_response(field: str, conn: dict) -> dict:
    return {"data": {"node": {field: conn}}}


def _install(monkeypatch, handler):
    calls: list[tuple[str, dict]] = []

    async def fake_run_query(query, variables, github_token=None):
        calls.append((query, dict(variables)))
        return handler(query, variables)

    monkeypatch.setattr(client, "run_query", fake_run_query)
    return calls


def _run(**kwargs):
    kwargs.setdefault("github_token", "test-token")
    return asyncio.run(client.fetch_prs("acme", "widget", **kwargs))


def test_thread_comments_overflow_is_paged_and_merged_in_order(monkeypatch):
    first = _conn([_comment(i) for i in range(1, 51)], has_next=True, end="cc1", total=120)
    pr = _pr(1, threads_conn=_conn([_thread("T1", first)], has_next=False))
    pages = {
        "cc1": _conn([_comment(i, bot=True) for i in range(51, 101)], has_next=True, end="cc2", total=120),
        "cc2": _conn([_comment(i) for i in range(101, 121)], has_next=False, end="cc3", total=120),
    }

    def handler(query, variables):
        if "GetThreadComments" in query:
            assert variables["id"] == "T1"
            return _node_response("comments", pages[variables["cursor"]])
        return _page([pr], has_next=False, end=None)

    calls = _install(monkeypatch, handler)
    (record,) = _run(pages=1)

    followups = [v for q, v in calls if "GetThreadComments" in q]
    assert [v["cursor"] for v in followups] == ["cc1", "cc2"]
    thread = record["threads"][0]
    assert [c["id"] for c in thread["comments"]] == [f"C{i}" for i in range(1, 121)]
    assert thread["comments_fetched"] == 120 and thread["comments_total"] == 120
    assert thread["thread_incomplete"] is False
    assert record["nested_incomplete_threads"] == 0
    assert "reviewThreads.comments" not in record["truncated_connections"]
    assert thread["comments"][50]["is_bot"] is True and thread["comments"][0]["is_bot"] is False
    assert len(record["review_comments"]) == 120  # legacy flat view sees the merged comments


def test_thread_comment_page_cap_marks_thread_incomplete(monkeypatch):
    first = _conn([_comment(1)], has_next=True, end="p0", total=10_000)
    pr = _pr(1, threads_conn=_conn([_thread("T1", first)], has_next=False))
    counter = {"n": 0}

    def handler(query, variables):
        if "GetThreadComments" in query:
            counter["n"] += 1
            n = counter["n"]
            return _node_response(
                "comments",
                _conn([_comment(1000 + n)], has_next=True, end=f"p{n}", total=10_000),
            )
        return _page([pr], has_next=False, end=None)

    _install(monkeypatch, handler)
    (record,) = _run(pages=1)

    assert counter["n"] == client.MAX_EXTRA_THREAD_COMMENT_PAGES == 10
    thread = record["threads"][0]
    assert thread["thread_incomplete"] is True
    assert thread["comments_fetched"] == 11 and thread["comments_total"] == 10_000
    assert record["nested_incomplete_threads"] == 1
    assert "reviewThreads.comments" in record["truncated_connections"]


def test_review_threads_overflow_is_paged_including_their_comment_overflow(monkeypatch):
    t1 = _thread("T1", _conn([_comment(1)], has_next=False, total=1))
    pr = _pr(1, threads_conn=_conn([t1], has_next=True, end="tt1", total=3))
    t2 = _thread("T2", _conn([_comment(2)], has_next=True, end="t2c", total=2))
    t3 = _thread("T3", _conn([_comment(3)], has_next=False, total=1))

    def handler(query, variables):
        if "GetPRReviewThreads" in query:
            assert variables == {"id": "PR_1", "cursor": "tt1"}
            return _node_response("reviewThreads", _conn([t2, t3], has_next=False, end="tt2", total=3))
        if "GetThreadComments" in query:
            assert variables == {"id": "T2", "cursor": "t2c"}
            return _node_response("comments", _conn([_comment(22)], has_next=False, end="x", total=2))
        return _page([pr], has_next=False, end=None)

    calls = _install(monkeypatch, handler)
    (record,) = _run(pages=1)

    assert [t["thread_id"] for t in record["threads"]] == ["T1", "T2", "T3"]
    assert [c["id"] for c in record["threads"][1]["comments"]] == ["C2", "C22"]
    assert record["nested_incomplete_threads"] == 0
    assert "reviewThreads" not in record["truncated_connections"]
    assert len(calls) == 3  # list page + one thread page + one comment page


def test_review_thread_page_cap_keeps_the_truncation_disclosure(monkeypatch):
    pr = _pr(1, threads_conn=_conn(
        [_thread("T0", _conn([_comment(1)], has_next=False, total=1))],
        has_next=True, end="t0", total=500))
    counter = {"n": 0}

    def handler(query, variables):
        if "GetPRReviewThreads" in query:
            counter["n"] += 1
            n = counter["n"]
            thread = _thread(f"T{n}", _conn([_comment(n + 1)], has_next=False, total=1))
            return _node_response("reviewThreads", _conn([thread], has_next=True, end=f"t{n}", total=500))
        return _page([pr], has_next=False, end=None)

    _install(monkeypatch, handler)
    (record,) = _run(pages=1)

    assert counter["n"] == client.MAX_EXTRA_REVIEW_THREAD_PAGES == 10
    assert len(record["threads"]) == 11
    assert "reviewThreads" in record["truncated_connections"]


def test_no_follow_up_queries_when_nothing_overflows(monkeypatch):
    pr = _pr(1, threads_conn=_conn(
        [_thread("T1", _conn([_comment(1)], has_next=False, total=1))], has_next=False))
    calls = _install(monkeypatch, lambda q, v: _page([pr], has_next=False, end=None))
    _run(pages=1)
    assert len(calls) == 1


# --- files / commits / reviews overflow --------------------------------------

def _file(n: int) -> dict:
    return {"path": f"src/f{n}.py", "additions": n, "deletions": 0, "changeType": "MODIFIED"}


def _commit(n: int) -> dict:
    return {"id": f"PRC_{n}", "commit": {
        "oid": f"oid{n}", "url": f"https://github.com/acme/widget/commit/oid{n}",
        "message": f"commit {n}", "author": {"name": "Dev", "user": {"login": "dev"}}}}


def _review(n: int) -> dict:
    return {"id": f"R{n}", "url": f"https://github.com/acme/widget/pull/1#r{n}",
            "author": {"__typename": "User", "login": "rev"}, "state": "APPROVED",
            "body": f"review {n}", "submittedAt": "2024-05-01T11:00:00Z",
            "updatedAt": "2024-05-01T11:00:00Z"}


# (field, query marker, record key, node factory, id of a node in a record, cap name)
_CASES = {
    "files": ("GetPRFiles", _file, lambda r: [f["path"] for f in r["files"]],
              lambda n: f"src/f{n}.py", "MAX_EXTRA_FILE_PAGES"),
    "commits": ("GetPRCommits", _commit, lambda r: [c["oid"] for c in r["commits"]],
                lambda n: f"oid{n}", "MAX_EXTRA_COMMIT_PAGES"),
    "reviews": ("GetPRReviews", _review, lambda r: [x["github_node_id"] for x in r["reviews"]],
                lambda n: f"R{n}", "MAX_EXTRA_REVIEW_PAGES"),
}


def _overflow_pr(field, make, nodes, **conn_kwargs):
    return _pr(1, threads_conn=_conn([], has_next=False),
               **{field: _conn([make(i) for i in nodes], **conn_kwargs)})


def test_files_commits_reviews_overflow_is_paged_and_merged_in_order(monkeypatch):
    for field, (marker, make, ids_of, ident, _cap) in _CASES.items():
        pr = _overflow_pr(field, make, range(1, 4), has_next=True, end="c1", total=8)
        pages = {
            "c1": _conn([make(i) for i in (4, 5)] + [make(3)], has_next=True, end="c2", total=8),
            "c2": _conn([make(i) for i in (6, 7, 8)], has_next=False, end="c3", total=8),
        }

        def handler(query, variables, marker=marker, field=field, pages=pages, pr=pr):
            if marker in query:
                assert variables["id"] == "PR_1"
                return _node_response(field, pages[variables["cursor"]])
            return _page([pr], has_next=False, end=None)

        calls = _install(monkeypatch, handler)
        (record,) = _run(pages=1)

        followups = [v for q, v in calls if marker in q]
        assert [v["cursor"] for v in followups] == ["c1", "c2"], field
        assert len(calls) == 3, field  # list page + exactly two follow-ups
        assert ids_of(record) == [ident(i) for i in range(1, 9)], field  # ordered, no repeat
        assert field not in record["truncated_connections"], field


def test_files_commits_reviews_cap_keeps_the_truncation_disclosure(monkeypatch):
    for field, (marker, make, ids_of, _ident, cap) in _CASES.items():
        pr = _overflow_pr(field, make, [0], has_next=True, end="p0", total=10_000)
        counter = {"n": 0}

        def handler(query, variables, marker=marker, field=field, pr=pr, make=make, counter=counter):
            if marker in query:
                counter["n"] += 1
                n = counter["n"]
                return _node_response(
                    field, _conn([make(n)], has_next=True, end=f"p{n}", total=10_000))
            return _page([pr], has_next=False, end=None)

        _install(monkeypatch, handler)
        (record,) = _run(pages=1)

        assert counter["n"] == getattr(client, cap) == 10, field
        assert len(ids_of(record)) == 11, field
        assert field in record["truncated_connections"], field


def test_overflow_cap_is_per_connection_not_shared(monkeypatch):
    pr = _pr(
        1, threads_conn=_conn([], has_next=False),
        files=_conn([_file(0)], has_next=True, end="f0", total=999),
        commits=_conn([_commit(0)], has_next=True, end="k0", total=999),
        reviews=_conn([_review(0)], has_next=False, total=1),
    )
    counters = {"GetPRFiles": 0, "GetPRCommits": 0}

    def handler(query, variables):
        for marker, field, make in (("GetPRFiles", "files", _file), ("GetPRCommits", "commits", _commit)):
            if marker in query:
                counters[marker] += 1
                n = counters[marker]
                return _node_response(field, _conn([make(n)], has_next=True, end=f"x{n}", total=999))
        return _page([pr], has_next=False, end=None)

    _install(monkeypatch, handler)
    (record,) = _run(pages=1)
    assert counters == {"GetPRFiles": 10, "GetPRCommits": 10}
    assert sorted(record["truncated_connections"]) == ["commits", "files"]


def test_no_follow_up_for_files_commits_reviews_when_nothing_overflows(monkeypatch):
    pr = _pr(
        1, threads_conn=_conn([], has_next=False),
        files=_conn([_file(1)], has_next=False, end="e", total=1),
        commits=_conn([_commit(1)], has_next=False, end="e", total=1),
        reviews=_conn([_review(1)], has_next=False, end="e", total=1),
    )
    calls = _install(monkeypatch, lambda q, v: _page([pr], has_next=False, end=None))
    (record,) = _run(pages=1)
    assert len(calls) == 1
    assert record["truncated_connections"] == []


def test_follow_up_without_cursor_or_node_keeps_disclosure(monkeypatch):
    pr = _pr(
        1, threads_conn=_conn([], has_next=False),
        files=_conn([_file(1)], has_next=True, end=None, total=500),  # no cursor: cannot page
        commits=_conn([_commit(1)], has_next=True, end="k", total=500),
    )

    def handler(query, variables):
        if "GetPRCommits" in query:
            return {"data": {"node": None}}  # node vanished
        return _page([pr], has_next=False, end=None)

    calls = _install(monkeypatch, handler)
    (record,) = _run(pages=1)
    assert len(calls) == 2  # list page + one commits attempt, none for files
    assert sorted(record["truncated_connections"]) == ["commits", "files"]


def test_follow_up_queries_target_pull_request_nodes_with_the_right_connection():
    from fetcher.queries import PR_COMMITS_QUERY, PR_FILES_QUERY, PR_REVIEWS_QUERY

    for query, field, name, size, needle in (
        (PR_FILES_QUERY, "files", "GetPRFiles", 100, "changeType"),
        (PR_COMMITS_QUERY, "commits", "GetPRCommits", 100, "oid"),
        (PR_REVIEWS_QUERY, "reviews", "GetPRReviews", 50, "submittedAt"),
    ):
        flat = " ".join(query.split())
        assert f"query {name}($id: ID!, $cursor: String)" in flat
        assert "node(id: $id) { ... on PullRequest {" in flat
        assert f"{field}(first: {size}, after: $cursor)" in flat
        assert "totalCount" in flat and "pageInfo { hasNextPage endCursor }" in flat
        assert needle in flat


def test_main_query_requests_cursor_and_total_for_files_commits_reviews():
    from fetcher.queries import PR_QUERY

    flat = " ".join(PR_QUERY.split())
    for field in ("files(first: 100)", "commits(first: 100)", "reviews(first: 50)"):
        start = flat.index(field)
        head = flat[start:start + 90]
        assert "totalCount" in head and "pageInfo { hasNextPage endCursor }" in head, field


def _created_pages():
    """Four PRs created newest-first (4,3,2,1), two per GraphQL page."""
    def mk(n):
        return _pr(n, threads_conn=_conn([], has_next=False),
                   created=f"2024-05-0{n}T10:00:00Z", updated="2024-06-01T00:00:00Z")
    return {
        None: _page([mk(4), mk(3)], has_next=True, end="after-3"),
        "after-3": _page([mk(2), mk(1)], has_next=False, end="after-1"),
    }


def test_created_at_window_returns_end_cursor_and_a_second_call_continues(monkeypatch):
    pages = _created_pages()
    queries: list[str] = []

    def handler(query, variables):
        queries.append(query)
        return pages[variables.get("cursor")]

    _install(monkeypatch, handler)

    first = _run(pages=1, order_by="CREATED_AT", return_result=True)
    assert [p["number"] for p in first.prs] == [4, 3]
    assert first.complete is False
    assert first.next_cursor == "after-3" and first.end_cursor == "after-3"
    assert all(p["truncated_connections"] == ["pullRequests"] for p in first.prs)

    second = _run(pages=1, order_by="CREATED_AT", after_cursor=first.end_cursor, return_result=True)
    assert [p["number"] for p in second.prs] == [2, 1]
    assert second.complete is True and second.next_cursor is None
    assert second.end_cursor == "after-1"

    assert not ({p["number"] for p in first.prs} & {p["number"] for p in second.prs})
    assert all("orderBy: {field: CREATED_AT, direction: DESC}" in " ".join(q.split()) for q in queries)


def test_created_at_ignores_since_filters_and_walks_all_pages(monkeypatch):
    pages = _created_pages()
    calls = _install(monkeypatch, lambda q, v: pages[v.get("cursor")])

    result = _run(
        pages=3, order_by="CREATED_AT", return_result=True,
        since_updated_at="2030-01-01T00:00:00Z",  # would stop immediately if honoured
    )
    assert [p["number"] for p in result.prs] == [4, 3, 2, 1]
    assert result.complete is True
    assert len(calls) == 2


def test_default_order_is_updated_at_and_unknown_order_is_rejected(monkeypatch):
    queries: list[str] = []

    def handler(query, variables):
        queries.append(query)
        return _page([], has_next=False, end=None)

    _install(monkeypatch, handler)
    _run(pages=1)
    assert "orderBy: {field: UPDATED_AT, direction: DESC}" in " ".join(queries[0].split())

    import pytest
    with pytest.raises(ValueError):
        _run(pages=1, order_by="STARS")


def test_records_carry_observed_at_and_end_cursor_with_updated_at_order(monkeypatch):
    pr = _pr(1, threads_conn=_conn([], has_next=False))
    _install(monkeypatch, lambda q, v: _page([pr], has_next=False, end="e1"))
    result = _run(pages=1, return_result=True)
    assert result.complete is True and result.next_cursor is None
    assert result.end_cursor == "e1"
    assert result.prs[0]["observed_at"].endswith("Z")
