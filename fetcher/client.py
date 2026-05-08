import httpx
import os
import sys
import time
import asyncio
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

class RateLimitError(Exception):
    """Raised when GitHub rate limit is exceeded."""
    def __init__(self, reset_at: int):
        self.reset_at = reset_at
        super().__init__(f"GitHub rate limit exceeded. Resets at {reset_at}.")

def _headers(github_token: str | None = None) -> dict:
    token = (github_token or "").strip() or os.getenv("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN is not set. Add it to your .env file.\n"
            "Get one at: https://github.com/settings/tokens (repo scope required)"
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

    if remaining is not None and int(remaining) < 100:
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
        retry=retry_if_exception_type((httpx.HTTPError, RateLimitError)),
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
                    raise PermissionError("GitHub 401: Invalid GITHUB_TOKEN.")
                
                resp.raise_for_status()
                data = resp.json()

                if "errors" in data:
                    errors = data["errors"]
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
    since_pr_number: int | None = None
) -> list[dict]:
    """
    Fetch up to pages*30 PRs. Supports incremental indexing via since_pr_number (Async).
    """
    if pages < 1:
        raise ValueError("pages must be at least 1.")
    if pages > MAX_PAGES:
        print(f"[*] pages capped at {MAX_PAGES} (requested {pages}).", file=sys.stderr)
        pages = MAX_PAGES

    all_prs = []
    cursor = None

    for page_num in range(1, pages + 1):
        variables = {"owner": owner, "repo": repo}
        if cursor:
            variables["cursor"] = cursor

        print(f"[*] Fetching page {page_num}/{pages} for {owner}/{repo}...", file=sys.stderr)
        data = await run_query(PR_QUERY, variables, github_token=github_token)
        pr_data = data["data"]["repository"]["pullRequests"]

        batch = flatten_prs(pr_data["nodes"])
        
        # Incremental check
        if since_pr_number:
            new_batch = [pr for pr in batch if pr["number"] > since_pr_number]
            all_prs.extend(new_batch)
            if len(new_batch) < len(batch):
                print(f"[+] Reached already indexed PRs (>{since_pr_number}). Stopping.", file=sys.stderr)
                break
        else:
            all_prs.extend(batch)

        page_info = pr_data["pageInfo"]
        if not page_info["hasPreviousPage"]:
            break
        cursor = page_info["startCursor"]

    return all_prs
