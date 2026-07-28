import httpx
import sys
import time
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tenacity import (
    AsyncRetrying,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from dotenv import load_dotenv
from fetcher.queries import PR_QUERY
from fetcher.transform import flatten_prs

load_dotenv()

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
MAX_PAGES = 10  # Hard cap to prevent accidental runaway fetches
REFRESH_OVERLAP = timedelta(minutes=5)


@dataclass(frozen=True)
class FetchResult:
    """One bounded GitHub pagination pass, including safe continuation state."""

    prs: list[dict]
    complete: bool
    next_cursor: str | None


class RateLimitError(Exception):
    """Raised when GitHub rate limit is exceeded."""
    def __init__(self, reset_at: int):
        self.reset_at = reset_at
        super().__init__(f"GitHub rate limit exceeded. Resets at {reset_at}.")

def _headers(github_token: str | None = None) -> dict:
    """Build request headers from a caller-supplied credential only.

    Credential discovery belongs to the local authorization boundary. Keeping it
    out of the transport layer prevents background work from silently reading a
    different environment token than the one the MCP connection selected.
    """
    token = (github_token or "").strip()
    if not token:
        raise EnvironmentError(
            "GitHub credentials are unavailable. Connect GitHub through the MCP "
            "authorization tools before indexing."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def _check_rate_limit(response: httpx.Response) -> None:
    """Warn if approaching GitHub's GraphQL rate limit or raise if exceeded."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset_at = response.headers.get("X-RateLimit-Reset")
    
    if response.status_code == 403 and remaining == "0":
        raise RateLimitError(int(reset_at or time.time() + 60))

    try:
        remaining_points = int(remaining) if remaining is not None else None
    except ValueError:
        remaining_points = None

    if remaining_points is not None and remaining_points < 100:
        print(
            f"[*] GitHub rate limit low: {remaining} points remaining. "
            f"Resets at unix timestamp {reset_at}.",
            file=sys.stderr
        )

async def run_query(query: str, variables: dict, github_token: str | None = None) -> dict:
    """Execute a raw GraphQL query with retries and rate limit handling (Async)."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        retry=retry_if_exception_type((httpx.HTTPError, RateLimitError, ConnectionError, TimeoutError)),
        reraise=True
    ):
        with attempt:
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.post(
                        GITHUB_GRAPHQL_URL,
                        json={"query": query, "variables": variables},
                        headers=_headers(github_token=github_token),
                        timeout=45,
                    )
                except httpx.ConnectError:
                    raise ConnectionError("Could not reach GitHub API. Check your connection.")
                except httpx.TimeoutException:
                    raise TimeoutError("GitHub API timed out. Try again or reduce --pages.")

                _check_rate_limit(resp)

                if resp.status_code == 401:
                    raise PermissionError(
                        "GitHub rejected the saved credential. Reconnect GitHub and try again."
                    )
                
                resp.raise_for_status()
                data = resp.json()

                if "errors" in data:
                    errors = data["errors"]
                    if any(e.get("type") in {"RATE_LIMITED", "RATE_LIMIT"} for e in errors):
                        reset_at = int(resp.headers.get("X-RateLimit-Reset") or time.time() + 60)
                        raise RateLimitError(reset_at)
                    if any(e.get("type") == "NOT_FOUND" for e in errors):
                        owner = variables.get("owner", "?")
                        repo = variables.get("repo", "?")
                        raise ValueError(f"Repository '{owner}/{repo}' not found or inaccessible.")
                    raise ValueError(f"GitHub GraphQL errors: {errors}")

                return data

async def fetch_prs(
    owner: str, 
    repo: str, 
    pages: int = 2, 
    github_token: str | None = None,
    since_pr_number: int | None = None,
    since_updated_at: str | None = None,
    after_cursor: str | None = None,
    return_result: bool = False,
) -> list[dict] | FetchResult:
    """
    Fetch up to pages*30 PRs ordered newest-first by GitHub's ``updatedAt``.

    ``since_updated_at`` is the preferred incremental watermark. A short overlap
    window intentionally refetches recently updated PRs; stable document IDs make
    the resulting upserts safe and prevent records updated at the boundary from
    being missed. If a bounded incremental pass hits the page cap,
    ``return_result=True`` exposes its next cursor so the caller can resume
    before committing the watermark. ``since_pr_number`` remains as a
    compatibility fallback for existing callers and old cursor databases.
    """
    if pages < 1:
        raise ValueError("pages must be at least 1.")
    if pages > MAX_PAGES:
        print(f"[*] pages capped at {MAX_PAGES} (requested {pages}).", file=sys.stderr)
        pages = MAX_PAGES

    all_prs = []
    cursor = after_cursor
    watermark = _parse_github_timestamp(since_updated_at) if since_updated_at else None
    overlap_cutoff = watermark - REFRESH_OVERLAP if watermark else None
    complete = False
    next_cursor = None

    for page_num in range(1, pages + 1):
        variables = {"owner": owner, "repo": repo}
        if cursor:
            variables["cursor"] = cursor

        print(f"[*] Fetching page {page_num}/{pages} for {owner}/{repo}...", file=sys.stderr)
        data = await run_query(PR_QUERY, variables, github_token=github_token)
        repository = data.get("data", {}).get("repository")
        if not repository:
            raise ValueError(f"Repository '{owner}/{repo}' not found or inaccessible.")
        pr_data = repository.get("pullRequests")
        if not pr_data:
            raise ValueError(f"GitHub returned no pull-request connection for '{owner}/{repo}'.")

        batch = flatten_prs(pr_data.get("nodes") or [])
        
        # UPDATED_AT pagination is newest-first. Keep a small overlap so an
        # update occurring while a previous sync was finishing cannot be lost.
        if overlap_cutoff:
            new_batch = [
                pr for pr in batch
                if _parse_github_timestamp(pr["updated_at"]) >= overlap_cutoff
            ]
            all_prs.extend(new_batch)
            if any(_parse_github_timestamp(pr["updated_at"]) < overlap_cutoff for pr in batch):
                print(f"[+] Reached refresh watermark ({since_updated_at}). Stopping.", file=sys.stderr)
                complete = True
                break
        elif since_pr_number:
            new_batch = [pr for pr in batch if pr["number"] > since_pr_number]
            all_prs.extend(new_batch)
            if len(new_batch) < len(batch):
                print(f"[+] Reached already indexed PRs (>{since_pr_number}). Stopping.", file=sys.stderr)
                complete = True
                break
        else:
            all_prs.extend(batch)

        page_info = pr_data["pageInfo"]
        if not page_info.get("hasNextPage"):
            complete = True
            break
        if page_num == pages:
            next_cursor = page_info.get("endCursor")
            if not next_cursor:
                raise ValueError("GitHub reported another PR page without an end cursor.")
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise ValueError("GitHub reported another PR page without an end cursor.")

    # A capped initial-history import is incomplete evidence and must disclose
    # that fact on the returned PR records. A capped incremental refresh is a
    # synchronization state instead: its caller persists ``next_cursor`` and
    # resumes before it advances the completed watermark.
    if not complete and not since_updated_at:
        for pr in all_prs:
            truncated = pr.setdefault("truncated_connections", [])
            if "pullRequests" not in truncated:
                truncated.append("pullRequests")

    result = FetchResult(prs=all_prs, complete=complete, next_cursor=next_cursor)
    return result if return_result else result.prs


def _parse_github_timestamp(value: str) -> datetime:
    """Parse GitHub's ISO-8601 timestamps into comparable UTC datetimes."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("GitHub returned a pull request without an updatedAt timestamp.")

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid GitHub timestamp: {value!r}") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
