# Raw GraphQL response → clean Python dicts.
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
    if not login: return False
    l = login.lower()
    return "[bot]" in l or l.endswith("-bot") or l in {"github-actions", "dependabot", "vercel"}

def flatten_pr(raw_pr: dict) -> dict:
    """Convert a single raw GraphQL PR node into a clean, flat dict with input guards."""
    files = [
        {
            "path": f["path"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "change_type": f["changeType"],
        }
        for f in raw_pr["files"]["nodes"]
    ]
    
    # Priority 3: CI/CD diff awareness
    touches_ci = any(
        any(pat in f["path"] for pat in CI_PATTERNS)
        for f in files
    )

    review_comments = []
    for thread in raw_pr["reviewThreads"]["nodes"]:
        for comment in thread["comments"]["nodes"]:
            diff_hunk = thread.get("diffHunk", "")
            if len(diff_hunk) > MAX_DIFF_SIZE:
                diff_hunk = diff_hunk[:MAX_DIFF_SIZE] + "\n... [diff truncated due to size] ..."

            author_login = comment["author"]["login"] if comment["author"] else "ghost"
            review_comments.append({
                "file": thread["path"],
                "line": thread["line"],
                "resolved": thread["isResolved"],
                "author": author_login,
                "is_bot": _is_bot(author_login),
                "body": comment["body"],
                "created_at": comment["createdAt"],
                "diff_hunk": diff_hunk,
            })

    body = raw_pr["body"] or ""
    if len(body) > MAX_BODY_SIZE:
        body = body[:MAX_BODY_SIZE] + "\n... [body truncated due to size] ..."

    author_login = raw_pr["author"]["login"] if raw_pr["author"] else "ghost"
    return {
        "number": raw_pr["number"],
        "title": raw_pr["title"],
        "body": body,
        "author": author_login,
        "author_is_bot": _is_bot(author_login),
        "touches_ci": touches_ci,
        "created_at": raw_pr["createdAt"],
        "merged_at": raw_pr["mergedAt"],
        "additions": raw_pr["additions"],
        "deletions": raw_pr["deletions"],
        "files": files,
        "review_comments": review_comments,
        "commits": [
            {"message": c["commit"]["message"]}
            for c in raw_pr["commits"]["nodes"]
        ],
        "reviews": [
            {
                "author": r["author"]["login"] if r["author"] else "ghost",
                "state": r["state"],
                "body": r["body"] or "",
                "submitted_at": r["submittedAt"],
            }
            for r in raw_pr["reviews"]["nodes"]
        ],
    }

def flatten_prs(nodes: list[dict]) -> list[dict]:
    """Flatten a list of raw GraphQL PR nodes."""
    return [flatten_pr(pr) for pr in nodes]
