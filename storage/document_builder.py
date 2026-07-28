# Converts raw PR dicts → text documents ready for embedding + storage.
# No model, no ChromaDB, no GitHub calls here.

import json


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
    if not isinstance(author, str):
        return False

    normalized_author = author.lower()
    return (
        "[bot]" in normalized_author
        or normalized_author.endswith("-bot")
        or normalized_author in {"github-actions", "dependabot", "vercel"}
    )


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


def build_documents(prs: list[dict]) -> tuple[list, list, list]:
    """
    Convert a list of PR dicts into (documents, metadatas, ids)
    ready to be encoded and upserted into ChromaDB.
    """
    docs, metadatas, ids = [], [], []

    for pr in prs:
        pr_num = pr["number"]
        extraction_metadata = _extraction_metadata(pr)
        extraction_note = _extraction_note(pr)

        # PR description
        if pr["body"].strip():
            docs.append(f"PR #{pr_num}: {pr['title']}\n{pr['body']}{extraction_note}")
            metadatas.append({
                "type": "pr_description",
                "pr_number": pr_num,
                "author": pr["author"],
                "author_is_bot": pr.get("author_is_bot", False),
                "touches_ci": pr.get("touches_ci", False),
                "files": json.dumps([f["path"] for f in pr["files"]]),
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
                "resolved": comment["resolved"],
                "touches_ci": pr.get("touches_ci", False),
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
                "touches_ci": pr.get("touches_ci", False),
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
                "touches_ci": pr.get("touches_ci", False),
                **extraction_metadata,
            })
            review_id = review.get("github_node_id") or str(i)
            ids.append(f"pr-{pr_num}-review-{review_id}")

    return docs, metadatas, ids
