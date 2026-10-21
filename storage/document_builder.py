# Converts raw PR dicts → text documents ready for embedding + storage.
# No model, no ChromaDB, no GitHub calls here.

import json

from fetcher.transform import author_type, _is_bot


def is_high_quality_comment(body: str, is_bot: bool) -> bool:
    """Filter out low-signal PR comments to improve embedding quality."""
    if is_bot:
        return False
        
    body_lower = body.strip().lower()
    
    # Too short to be a substantive review comment
    if len(body_lower) < 15:
        return False

    # A "nit:" prefix flags a deliberately minor remark, so it has to carry more
    # substance than the general minimum before it earns a place in the index.
    if body_lower.startswith("nit:") and len(body_lower) < 25:
        return False
        
    # Common low-signal phrases
    low_signal_phrases = [
        "lgtm", "looks good", "looks good to me", "+1", "nit:", "agreed", 
        "done", "fixed", "thanks", "bump", "ping", "addressed"
    ]
    
    # If the comment is just a short variation of a low-signal phrase
    if any(body_lower == p or (body_lower.startswith(p) and len(body_lower) < len(p) + 10) for p in low_signal_phrases):
        return False
        
    return True

def _review_is_bot(review: dict) -> bool:
    """Return a reviewer's bot flag without relying on an inline comment."""
    if "is_bot" in review:
        return bool(review["is_bot"])

    author = review.get("author")
    return _is_bot(author) if isinstance(author, str) else False


def _pr_url(pr: dict, repo_key: str | None) -> str:
    """Prefer the URL GitHub returned; derive the canonical one only as a fallback."""
    url = pr.get("url")
    if isinstance(url, str) and url:
        return url
    return f"https://github.com/{repo_key}/pull/{pr['number']}" if repo_key else ""


def _links(pr_url: str, review_url: str = "", comment_url: str = "") -> dict:
    """Evidence links stored as flat strings; '' means GitHub gave no such link."""
    return {"pr_url": pr_url, "review_url": review_url, "comment_url": comment_url}


def _extraction_metadata(pr: dict) -> dict:
    """Preserve whether GitHub returned a complete nested PR record."""
    truncated = pr.get("truncated_connections") or []
    if not isinstance(truncated, list):
        truncated = []

    return {
        "source_truncated": bool(truncated),
        "truncated_connections": json.dumps(truncated),
        "updated_at": pr.get("updated_at", ""),
    }


def _extraction_note(pr: dict) -> str:
    truncated = pr.get("truncated_connections") or []
    if not isinstance(truncated, list) or not truncated:
        return ""
    return (
        "\n[Extraction note: GitHub paginated only part of this PR's "
        f"{', '.join(str(value) for value in truncated)} connection(s).]"
    )


def build_documents(
    prs: list[dict], repo_key: str | None = None
) -> tuple[list, list, list]:
    """
    Convert a list of PR dicts into (documents, metadatas, ids)
    ready to be encoded and upserted into ChromaDB.

    Every document carries ``author_type`` ("human" or "bot") and the evidence
    links (``pr_url``, ``review_url``, ``comment_url``) that GitHub supplied.
    ``repo_key`` lets the PR link be derived for records fetched before URLs
    were requested.
    """
    docs, metadatas, ids = [], [], []

    for pr in prs:
        pr_num = pr["number"]
        extraction_metadata = _extraction_metadata(pr)
        extraction_note = _extraction_note(pr)
        pr_url = _pr_url(pr, repo_key)
        pr_author_is_bot = bool(pr.get("author_is_bot", False))

        # PR description
        if pr["body"].strip():
            docs.append(f"PR #{pr_num}: {pr['title']}\n{pr['body']}{extraction_note}")
            metadatas.append({
                "type": "pr_description",
                "pr_number": pr_num,
                "author": pr["author"],
                "author_is_bot": pr_author_is_bot,
                "author_type": author_type(pr_author_is_bot),
                "touches_ci": pr.get("touches_ci", False),
                "files": json.dumps([f["path"] for f in pr["files"]]),
                **_links(pr_url),
                **extraction_metadata,
            })
            ids.append(f"pr-{pr_num}-desc")

        # Inline review comments + code context
        for i, comment in enumerate(pr["review_comments"]):
            if not is_high_quality_comment(comment["body"], comment.get("is_bot", False)):
                continue
            
            diff_text = f"\nCode Context:\n{comment['diff_hunk']}" if comment.get("diff_hunk") else ""
            docs.append(
                f"PR #{pr_num} | File: {comment['file']} | Line: {comment['line']}{diff_text}\n"
                f"Reviewer ({comment['author']}): {comment['body']}{extraction_note}"
            )
            metadatas.append({
                "type": "review_comment",
                "pr_number": pr_num,
                "file": comment["file"],
                "author": comment["author"],
                "author_type": author_type(bool(comment.get("is_bot", False))),
                "resolved": comment["resolved"],
                "touches_ci": pr.get("touches_ci", False),
                **_links(pr_url, comment.get("review_url") or "", comment.get("url") or ""),
                **extraction_metadata,
            })
            comment_id = comment.get("github_node_id") or str(i)
            ids.append(f"pr-{pr_num}-comment-{comment_id}")

        # Commit messages
        for i, commit in enumerate(pr.get("commits", [])):
            if not commit["message"].strip():
                continue
            docs.append(f"PR #{pr_num} Commit: {commit['message']}{extraction_note}")
            metadatas.append({
                "type": "commit_message",
                "pr_number": pr_num,
                # Commit authors are not fetched, so a commit inherits its PR
                # author's type (a dependabot PR's commits are bot commits).
                "author_type": author_type(pr_author_is_bot),
                "touches_ci": pr.get("touches_ci", False),
                **_links(pr_url),
                **extraction_metadata,
            })
            commit_id = commit.get("oid") or str(i)
            ids.append(f"pr-{pr_num}-commit-{commit_id}")

        # Overall review summaries (only those with written body)
        for i, review in enumerate(pr["reviews"]):
            # Bot review summaries are deliberately indexed (they are tagged via the
            # `is_bot` metadata below so callers can filter downstream), so the bot
            # rejection in is_high_quality_comment is bypassed here. Only the
            # length/low-signal checks apply to review summaries.
            if not is_high_quality_comment(review["body"], False):
                continue
            docs.append(
                f"PR #{pr_num} overall review by {review['author']} "
                f"[{review['state']}]: {review['body']}{extraction_note}"
            )
            metadatas.append({
                "type": "review_summary",
                "pr_number": pr_num,
                "state": review["state"],
                "author": review["author"],
                "is_bot": _review_is_bot(review),
                "author_type": author_type(_review_is_bot(review)),
                "touches_ci": pr.get("touches_ci", False),
                **_links(pr_url, review.get("url") or ""),
                **extraction_metadata,
            })
            review_id = review.get("github_node_id") or str(i)
            ids.append(f"pr-{pr_num}-review-{review_id}")

    return docs, metadatas, ids
