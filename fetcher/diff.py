"""
PR diff fetcher: fetch an open or closed GitHub PR diff via the REST API.
Returns per-file, per-hunk structured data ready for per-file review context.
Uses the existing httpx + GITHUB_TOKEN infrastructure.
"""
from __future__ import annotations
import re
import os
import sys
import httpx
from typing import Generator


MAX_HUNK_CHARS = 4000  # keep LLM context manageable per hunk


def _parse_pr_identifier(pr_ref: str) -> tuple[str, str, int]:
    """
    Parse a PR reference into (owner, repo, pr_number).
    Accepts formats:
      - "owner/repo#123"
      - "https://github.com/owner/repo/pull/123"
    """
    # URL format
    url_match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_ref
    )
    if url_match:
        return url_match.group(1), url_match.group(2), int(url_match.group(3))

    # Short format owner/repo#123
    short_match = re.match(r"([^/]+)/([^#]+)#(\d+)", pr_ref)
    if short_match:
        return short_match.group(1), short_match.group(2), int(short_match.group(3))

    raise ValueError(
        f"Cannot parse PR reference: '{pr_ref}'. "
        "Use 'owner/repo#123' or 'https://github.com/owner/repo/pull/123'."
    )


def _parse_unified_diff(raw_diff: str) -> list[dict]:
    """
    Parse a unified diff string into a list of file hunks.
    Each entry:
      - file: str          — filename (new path)
      - change_type: str   — ADDED | REMOVED | MODIFIED | RENAMED
      - hunks: list[str]   — raw hunk text chunks
    """
    files: list[dict] = []
    current_file: dict | None = None
    current_hunk_lines: list[str] = []

    for line in raw_diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            # Flush previous hunk
            if current_file is not None and current_hunk_lines:
                current_file["hunks"].append("".join(current_hunk_lines))
                current_hunk_lines = []
            current_file = {"file": "", "change_type": "MODIFIED", "hunks": []}
            files.append(current_file)

        elif line.startswith("new file mode") and current_file:
            current_file["change_type"] = "ADDED"

        elif line.startswith("deleted file mode") and current_file:
            current_file["change_type"] = "REMOVED"

        elif line.startswith("rename to") and current_file:
            current_file["change_type"] = "RENAMED"

        elif line.startswith("+++ b/") and current_file:
            current_file["file"] = line[6:].strip()

        elif line.startswith("+++ /dev/null") and current_file:
            # Deletion — filename comes from --- a/
            pass

        elif line.startswith("--- ") and current_file:
            # Ignore --- a/ metadata lines — not part of hunk content
            pass

        elif line.startswith("index ") and current_file:
            # Skip index SHA metadata lines
            pass

        elif line.startswith("@@") and current_file:
            # Flush previous hunk
            if current_hunk_lines:
                current_file["hunks"].append("".join(current_hunk_lines))
            current_hunk_lines = [line]

        elif current_file is not None:
            current_hunk_lines.append(line)

    # Flush last hunk
    if current_file is not None and current_hunk_lines:
        current_file["hunks"].append("".join(current_hunk_lines))

    # Remove empty file entries (e.g. binary files)
    return [f for f in files if f["file"] and f["hunks"]]


async def fetch_pr_diff(
    pr_ref: str,
    github_token: str | None = None,
) -> list[dict]:
    """
    Fetch a GitHub PR diff and return structured per-file, per-hunk data.

    Args:
        pr_ref: A PR reference like 'owner/repo#123' or a full GitHub URL.
        github_token: Optional GitHub PAT. Falls back to GITHUB_TOKEN env var.

    Returns:
        A list of file dicts:
          [
            {
              "file": "src/foo.py",
              "change_type": "MODIFIED",
              "hunks": ["@@ -10,7 +10,9 @@ ...", ...]
            },
            ...
          ]
    """
    owner, repo, pr_number = _parse_pr_identifier(pr_ref)
    token = (github_token or "").strip() or os.getenv("GITHUB_TOKEN", "")

    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=45) as client:
        resp = await client.get(url, headers=headers)

        if resp.status_code == 404:
            raise ValueError(f"PR not found: {owner}/{repo}#{pr_number}")
        if resp.status_code == 401:
            raise PermissionError("GitHub 401: Invalid or missing GITHUB_TOKEN.")
        resp.raise_for_status()

    raw_diff = resp.text
    return _parse_unified_diff(raw_diff)
