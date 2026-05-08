# Converts raw PR dicts → text documents ready for embedding + storage.
# No model, no ChromaDB, no GitHub calls here.

import json


def build_documents(prs: list[dict]) -> tuple[list, list, list]:
    """
    Convert a list of PR dicts into (documents, metadatas, ids)
    ready to be encoded and upserted into ChromaDB.
    """
    docs, metadatas, ids = [], [], []

    for pr in prs:
        pr_num = pr["number"]

        # PR description
        if pr["body"].strip():
            docs.append(f"PR #{pr_num}: {pr['title']}\n{pr['body']}")
            metadatas.append({
                "type": "pr_description",
                "pr_number": pr_num,
                "author": pr["author"],
                "author_is_bot": pr.get("author_is_bot", False),
                "touches_ci": pr.get("touches_ci", False),
                "files": json.dumps([f["path"] for f in pr["files"]]),
            })
            ids.append(f"pr-{pr_num}-desc")

        # Inline review comments + code context
        for i, comment in enumerate(pr["review_comments"]):
            if not comment["body"].strip():
                continue
            
            diff_text = f"\nCode Context:\n{comment['diff_hunk']}" if comment.get("diff_hunk") else ""
            docs.append(
                f"PR #{pr_num} | File: {comment['file']} | Line: {comment['line']}{diff_text}\n"
                f"Reviewer ({comment['author']}): {comment['body']}"
            )
            metadatas.append({
                "type": "review_comment",
                "pr_number": pr_num,
                "file": comment["file"],
                "author": comment["author"],
                "is_bot": comment.get("is_bot", False),
                "resolved": comment["resolved"],
                "touches_ci": pr.get("touches_ci", False),
            })
            ids.append(f"pr-{pr_num}-comment-{i}")

        # Commit messages
        for i, commit in enumerate(pr.get("commits", [])):
            if not commit["message"].strip():
                continue
            docs.append(f"PR #{pr_num} Commit: {commit['message']}")
            metadatas.append({
                "type": "commit_message",
                "pr_number": pr_num,
                "touches_ci": pr.get("touches_ci", False),
            })
            ids.append(f"pr-{pr_num}-commit-{i}")

        # Overall review summaries (only those with written body)
        for i, review in enumerate(pr["reviews"]):
            if not review["body"].strip():
                continue
            docs.append(
                f"PR #{pr_num} overall review by {review['author']} "
                f"[{review['state']}]: {review['body']}"
            )
            metadatas.append({
                "type": "review_summary",
                "pr_number": pr_num,
                "state": review["state"],
                "author": review["author"],
                "is_bot": comment.get("is_bot", False), # Approximation
                "touches_ci": pr.get("touches_ci", False),
            })
            ids.append(f"pr-{pr_num}-review-{i}")

    return docs, metadatas, ids
