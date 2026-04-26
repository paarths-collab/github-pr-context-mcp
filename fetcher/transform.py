# Raw GraphQL response → clean Python dicts.
# No HTTP calls, no ChromaDB, no embedding logic here.

def flatten_pr(raw_pr: dict) -> dict:
    """Convert a single raw GraphQL PR node into a clean, flat dict."""
    review_comments = []
    for thread in raw_pr["reviewThreads"]["nodes"]:
        for comment in thread["comments"]["nodes"]:
            review_comments.append({
                "file": thread["path"],
                "line": thread["line"],
                "resolved": thread["isResolved"],
                "author": comment["author"]["login"] if comment["author"] else "ghost",
                "body": comment["body"],
                "created_at": comment["createdAt"],
                "diff_hunk": thread.get("diffHunk", ""),
            })

    return {
        "number": raw_pr["number"],
        "title": raw_pr["title"],
        "body": raw_pr["body"] or "",
        "author": raw_pr["author"]["login"] if raw_pr["author"] else "ghost",
        "created_at": raw_pr["createdAt"],
        "merged_at": raw_pr["mergedAt"],
        "additions": raw_pr["additions"],
        "deletions": raw_pr["deletions"],
        "files": [
            {
                "path": f["path"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "change_type": f["changeType"],
            }
            for f in raw_pr["files"]["nodes"]
        ],
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
