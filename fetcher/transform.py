# Raw GraphQL response -> clean Python dicts.
# No HTTP calls, no ChromaDB, no embedding logic here.

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


def flatten_pr(raw_pr: dict) -> dict:
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
    raw_threads = _connection_nodes(
        raw_pr.get("reviewThreads"), "reviewThreads", truncated_connections
    )
    for thread in raw_threads:
        if not isinstance(thread, dict):
            continue
        raw_comments = _connection_nodes(
            thread.get("comments"), "reviewThreads.comments", truncated_connections
        )
        diff_hunk = _text(thread.get("diffHunk"))
        if len(diff_hunk) > MAX_DIFF_SIZE:
            diff_hunk = diff_hunk[:MAX_DIFF_SIZE] + "\n... [diff truncated due to size] ..."

        for comment in raw_comments:
            if not isinstance(comment, dict):
                continue
            author_login = _login(comment.get("author"))
            review_comments.append({
                "github_node_id": _text(comment.get("id")),
                "file": _text(thread.get("path")),
                "line": thread.get("line"),
                "resolved": bool(thread.get("isResolved")),
                "author": author_login,
                "is_bot": _is_bot(author_login),
                "body": _text(comment.get("body")),
                "created_at": _text(comment.get("createdAt")),
                "diff_hunk": diff_hunk,
            })

    body = _text(raw_pr.get("body"))
    if len(body) > MAX_BODY_SIZE:
        body = body[:MAX_BODY_SIZE] + "\n... [body truncated due to size] ..."

    author_login = _login(raw_pr.get("author"))
    raw_commits = _connection_nodes(raw_pr.get("commits"), "commits", truncated_connections)
    commits = []
    for commit_node in raw_commits:
        commit = commit_node.get("commit") if isinstance(commit_node, dict) else None
        if isinstance(commit, dict):
            commits.append({
                "oid": _text(commit.get("oid")),
                "message": _text(commit.get("message")),
            })

    raw_reviews = _connection_nodes(raw_pr.get("reviews"), "reviews", truncated_connections)
    reviews = []
    for review in raw_reviews:
        if not isinstance(review, dict):
            continue
        reviewer = _login(review.get("author"))
        reviews.append({
            "github_node_id": _text(review.get("id")),
            "author": reviewer,
            "is_bot": _is_bot(reviewer),
            "state": _text(review.get("state")),
            "body": _text(review.get("body")),
            "submitted_at": _text(review.get("submittedAt")),
        })

    created_at = _text(raw_pr.get("createdAt"))
    updated_at = _text(raw_pr.get("updatedAt")) or created_at
    return {
        "github_node_id": _text(raw_pr.get("id")),
        "number": raw_pr["number"],
        "title": _text(raw_pr.get("title")),
        "body": body,
        "author": author_login,
        "author_is_bot": _is_bot(author_login),
        "touches_ci": touches_ci,
        "created_at": created_at,
        "updated_at": updated_at,
        "merged_at": _text(raw_pr.get("mergedAt")) or None,
        "state": _text(raw_pr.get("state")),
        "additions": raw_pr.get("additions", 0),
        "deletions": raw_pr.get("deletions", 0),
        "files": files,
        "review_comments": review_comments,
        "commits": commits,
        "reviews": reviews,
        "truncated_connections": truncated_connections,
    }


def flatten_prs(nodes: list[dict]) -> list[dict]:
    """Flatten a list of raw GitHub PR nodes."""
    return [flatten_pr(pr) for pr in nodes]
