# HTTP client for the GitHub GraphQL API.
# Handles: auth, pagination, rate limit detection, and user-friendly errors.

import requests
import os
from dotenv import load_dotenv
from fetcher.queries import PR_QUERY
from fetcher.transform import flatten_prs

load_dotenv()

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
MAX_PAGES = 10  # Hard cap to prevent accidental runaway fetches


def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN is not set. Add it to your .env file.\n"
            "Get one at: https://github.com/settings/tokens (repo scope required)"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _check_rate_limit(response: requests.Response) -> None:
    """Warn if approaching GitHub's GraphQL rate limit."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) < 100:
        reset_at = response.headers.get("X-RateLimit-Reset", "unknown")
        print(
            f"⚠️  GitHub rate limit low: {remaining} points remaining. "
            f"Resets at unix timestamp {reset_at}."
        )


def run_query(query: str, variables: dict) -> dict:
    """Execute a raw GraphQL query against the GitHub API."""
    try:
        resp = requests.post(
            GITHUB_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=_headers(),
            timeout=30,
        )
    except requests.exceptions.ConnectionError:
        raise ConnectionError(
            "Could not reach GitHub API. Check your internet connection."
        )
    except requests.exceptions.Timeout:
        raise TimeoutError(
            "GitHub API timed out after 30s. Try again or reduce --pages."
        )

    _check_rate_limit(resp)

    # Surface actionable errors instead of raw HTTP codes
    if resp.status_code == 401:
        raise PermissionError(
            "GitHub returned 401 Unauthorized. Your GITHUB_TOKEN is invalid or expired.\n"
            "Generate a new one at: https://github.com/settings/tokens"
        )
    if resp.status_code == 403:
        raise PermissionError(
            "GitHub returned 403 Forbidden. Your token may lack 'repo' scope, "
            "or you've exceeded the rate limit."
        )

    resp.raise_for_status()
    data = resp.json()

    if "errors" in data:
        errors = data["errors"]
        # Repo not found is the most common user error — give a specific message
        if any(e.get("type") == "NOT_FOUND" for e in errors):
            owner = variables.get("owner", "?")
            repo = variables.get("repo", "?")
            raise ValueError(
                f"Repository '{owner}/{repo}' not found or not accessible with your token. "
                "Check the owner/repo spelling and that your token has 'repo' scope."
            )
        raise ValueError(f"GitHub GraphQL errors: {errors}")

    return data


def fetch_prs(owner: str, repo: str, pages: int = 2) -> list[dict]:
    """
    Fetch up to pages*30 merged/closed PRs with all review context.

    Args:
        owner: GitHub username or org, e.g. 'psf'
        repo:  Repository name, e.g. 'black'
        pages: Number of pages to fetch (30 PRs per page).
               Capped at MAX_PAGES={MAX_PAGES} to prevent runaway fetches.

    Returns:
        List of flattened PR dicts with review comments.
    """
    if pages < 1:
        raise ValueError("pages must be at least 1.")
    if pages > MAX_PAGES:
        print(f"⚠️  pages capped at {MAX_PAGES} (requested {pages}).")
        pages = MAX_PAGES

    all_prs = []
    cursor = None

    for page_num in range(1, pages + 1):
        variables = {"owner": owner, "repo": repo}
        if cursor:
            variables["cursor"] = cursor

        print(f"  Fetching page {page_num}/{pages} for {owner}/{repo}...")
        data = run_query(PR_QUERY, variables)
        pr_data = data["data"]["repository"]["pullRequests"]

        batch = flatten_prs(pr_data["nodes"])
        all_prs.extend(batch)

        page_info = pr_data["pageInfo"]
        if not page_info["hasPreviousPage"]:
            break
        cursor = page_info["startCursor"]

    return all_prs
