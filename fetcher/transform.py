# Raw GraphQL response -> clean Python dicts.
# No HTTP calls, no ChromaDB, no embedding logic here.

from datetime import datetime, timezone

MAX_DIFF_SIZE = 100 * 1024  # 100KB per hunk
MAX_BODY_SIZE = 50 * 1024   # 50KB per PR body

CI_PATTERNS = [
    ".github/workflows/",
    "Dockerfile",
    "docker-compose",
    "terraform/",
    "k8s/",
    "kubernetes/",
    "helm/",
    ".gitlab-ci.yml",
    "jenkinsfile",
]


def _is_bot(login: str) -> bool:
    """Heuristic to detect if a login belongs to a bot."""
    if not login:
        return False
    normalized = login.lower()
    return (
        "[bot]" in normalized
        or normalized.endswith("-bot")
        or normalized in {"github-actions", "dependabot", "vercel"}
    )


def _text(value: object) -> str:
    """Return API text safely without turning nulls into the literal 'None'."""
    return value if isinstance(value, str) else ""


def _connection_nodes(connection: object, path: str, truncated: list[str]) -> list[dict]:
    """Read a nullable GitHub connection and disclose a nested page cap."""
    if not isinstance(connection, dict):
        return []

    page_info = connection.get("pageInfo")
    if isinstance(page_info, dict) and page_info.get("hasNextPage") and path not in truncated:
        truncated.append(path)

    nodes = connection.get("nodes")
    return nodes if isinstance(nodes, list) else []


def _login(actor: object) -> str:
    if isinstance(actor, dict):
        login = actor.get("login")
        if isinstance(login, str) and login:
            return login
    return "ghost"


def _author(actor: object) -> tuple[str, str, bool]:
    """Return ``(login, author_type, is_bot)`` for a GraphQL Actor node.

    ``__typename == "Bot"`` is authoritative; otherwise the login heuristic
    applies. A null author (deleted account) is ``ghost`` / ``unknown``.
    """
    login = _login(actor)
    if login == "ghost" and not (isinstance(actor, dict) and actor.get("login")):
        return "ghost", "unknown", False
    typename = actor.get("__typename") if isinstance(actor, dict) else None
    if typename == "Bot" or _is_bot(login):
        return login, "bot", True
    return login, "human", False


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def now_utc_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision, ``Z`` suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def flatten_pr(raw_pr: dict, observed_at: str | None = None) -> dict:
    """Convert a raw GitHub PR node into a guarded, disclosure-rich record."""
    if not isinstance(raw_pr, dict):
        raise ValueError("GitHub returned a pull request node that is not an object.")

    truncated_connections: list[str] = []
    raw_files = _connection_nodes(raw_pr.get("files"), "files", truncated_connections)
    files = [
        {
            "path": _text(file.get("path")),
            "additions": file.get("additions", 0),
            "deletions": file.get("deletions", 0),
            "change_type": _text(file.get("changeType")),
        }
        for file in raw_files
        if isinstance(file, dict)
    ]

    touches_ci = any(
        any(pattern in file["path"] for pattern in CI_PATTERNS)
        for file in files
    )

    review_comments = []
    threads = []
    raw_threads = _connection_nodes(
        raw_pr.get("reviewThreads"), "reviewThreads", truncated_connections
    )
    for thread in raw_threads:
        if not isinstance(thread, dict):
            continue
        raw_comments = _connection_nodes(
            thread.get("comments"), "reviewThreads.comments", truncated_connections
        )
        # GitHub exposes diffHunk on the review *comment*, not on the thread. A
        # reply in the thread can omit it, so the first anchored hunk in the
        # thread stands in for the whole conversation.
        thread_hunk = ""
        for comment in raw_comments:
            if isinstance(comment, dict) and _text(comment.get("diffHunk")):
                thread_hunk = _text(comment.get("diffHunk"))
                break

        thread_comments = []
        for comment in raw_comments:
            if not isinstance(comment, dict):
                continue
            diff_hunk = _text(comment.get("diffHunk")) or thread_hunk
            if len(diff_hunk) > MAX_DIFF_SIZE:
                diff_hunk = diff_hunk[:MAX_DIFF_SIZE] + "\n... [diff truncated due to size] ..."
            author_login, c_type, c_bot = _author(comment.get("author"))
            review_comments.append({
                "github_node_id": _text(comment.get("id")),
                "file": _text(thread.get("path")),
                "line": thread.get("line"),
                "resolved": bool(thread.get("isResolved")),
                "author": author_login,
                "is_bot": c_bot,
                "body": _text(comment.get("body")),
                "created_at": _text(comment.get("createdAt")),
                "diff_hunk": diff_hunk,
            })
            thread_comments.append({
                "id": _text(comment.get("id")),
                "database_id": _int_or_none(comment.get("databaseId")),
                "url": _text(comment.get("url")),
                "author": author_login,
                "author_type": c_type,
                "is_bot": c_bot,
                "body": _text(comment.get("body")),
                "created_at": _text(comment.get("createdAt")),
                "updated_at": _text(comment.get("updatedAt")) or _text(comment.get("createdAt")),
                "diff_hunk": diff_hunk,
            })

        comments_conn = thread.get("comments")
        comments_page = comments_conn.get("pageInfo") if isinstance(comments_conn, dict) else None
        has_more = bool(isinstance(comments_page, dict) and comments_page.get("hasNextPage"))
        total = _int_or_none(comments_conn.get("totalCount")) if isinstance(comments_conn, dict) else None
        fetched = len(thread_comments)
        first_raw = next((c for c in raw_comments if isinstance(c, dict)), {})
        original_commit = first_raw.get("originalCommit")
        original_oid = _text(original_commit.get("oid")) if isinstance(original_commit, dict) else ""
        threads.append({
            "thread_id": _text(thread.get("id")),
            "url": _text(first_raw.get("url")),
            "path": _text(thread.get("path")),
            "line": _int_or_none(thread.get("line")),
            "original_line": _int_or_none(thread.get("originalLine")),
            "original_commit": original_oid or None,
            "is_resolved": bool(thread.get("isResolved")),
            "is_outdated": bool(thread.get("isOutdated")),
            "comments": thread_comments,
            "comments_fetched": fetched,
            "comments_total": total,
            "thread_incomplete": has_more or (total is not None and fetched < total),
        })

    body = _text(raw_pr.get("body"))
    if len(body) > MAX_BODY_SIZE:
        body = body[:MAX_BODY_SIZE] + "\n... [body truncated due to size] ..."

    author_login, author_type, author_is_bot = _author(raw_pr.get("author"))
    raw_commits = _connection_nodes(raw_pr.get("commits"), "commits", truncated_connections)
    commits = []
    for commit_node in raw_commits:
        commit = commit_node.get("commit") if isinstance(commit_node, dict) else None
        if isinstance(commit, dict):
            commit_author = commit.get("author")
            commit_user = commit_author.get("user") if isinstance(commit_author, dict) else None
            commits.append({
                "oid": _text(commit.get("oid")),
                "message": _text(commit.get("message")),
                "url": _text(commit.get("url")),
                "author": _text(commit_user.get("login")) if isinstance(commit_user, dict) else "",
            })

    raw_reviews = _connection_nodes(raw_pr.get("reviews"), "reviews", truncated_connections)
    reviews = []
    for review in raw_reviews:
        if not isinstance(review, dict):
            continue
        reviewer, reviewer_type, reviewer_bot = _author(review.get("author"))
        reviews.append({
            "github_node_id": _text(review.get("id")),
            "url": _text(review.get("url")),
            "author": reviewer,
            "author_type": reviewer_type,
            "is_bot": reviewer_bot,
            "updated_at": _text(review.get("updatedAt")) or _text(review.get("submittedAt")),
            "state": _text(review.get("state")),
            "body": _text(review.get("body")),
            "submitted_at": _text(review.get("submittedAt")),
        })

    created_at = _text(raw_pr.get("createdAt"))
    updated_at = _text(raw_pr.get("updatedAt")) or created_at
    return {
        "github_node_id": _text(raw_pr.get("id")),
        "number": raw_pr["number"],
        "url": _text(raw_pr.get("url")),
        "title": _text(raw_pr.get("title")),
        "body": body,
        "author": author_login,
        "author_is_bot": author_is_bot,
        "author_type": author_type,
        "is_bot": author_is_bot,
        "observed_at": observed_at or now_utc_iso(),
        "touches_ci": touches_ci,
        "created_at": created_at,
        "updated_at": updated_at,
        "merged_at": _text(raw_pr.get("mergedAt")) or None,
        "state": _text(raw_pr.get("state")),
        "additions": raw_pr.get("additions", 0),
        "deletions": raw_pr.get("deletions", 0),
        "files": files,
        "review_comments": review_comments,
        "threads": threads,
        "nested_incomplete_threads": sum(1 for t in threads if t["thread_incomplete"]),
        "commits": commits,
        "reviews": reviews,
        "truncated_connections": truncated_connections,
    }


def flatten_prs(nodes: list[dict], observed_at: str | None = None) -> list[dict]:
    """Flatten a list of raw GitHub PR nodes."""
    stamp = observed_at or now_utc_iso()
    return [flatten_pr(pr, observed_at=stamp) for pr in nodes]
