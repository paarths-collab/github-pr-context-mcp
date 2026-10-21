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
from fetcher.queries import (
    PR_COMMITS_QUERY,
    PR_FILES_QUERY,
    PR_ORDER_FIELDS,
    PR_QUERY,
    PR_QUERY_CREATED_AT,
    PR_REVIEW_THREADS_QUERY,
    PR_REVIEWS_QUERY,
    THREAD_COMMENTS_QUERY,
)
from fetcher.transform import flatten_prs, now_utc_iso

load_dotenv()

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
MAX_PAGES = 10  # Hard cap to prevent accidental runaway fetches
REFRESH_OVERLAP = timedelta(minutes=5)
MAX_EXTRA_THREAD_COMMENT_PAGES = 10  # extra comment pages fetched per review thread
MAX_EXTRA_REVIEW_THREAD_PAGES = 10   # extra reviewThreads pages fetched per PR
MAX_EXTRA_FILE_PAGES = 10            # extra files pages fetched per PR
MAX_EXTRA_COMMIT_PAGES = 10          # extra commits pages fetched per PR
MAX_EXTRA_REVIEW_PAGES = 10          # extra reviews pages fetched per PR
_QUERIES_BY_ORDER = {"UPDATED_AT": PR_QUERY, "CREATED_AT": PR_QUERY_CREATED_AT}


@dataclass(frozen=True)
class FetchResult:
    """One bounded GitHub pagination pass, including safe continuation state."""

    prs: list[dict]
    complete: bool
    next_cursor: str | None
    # Cursor after the last PR page fetched, even when ``complete`` is False
    # (usable as ``after_cursor`` for a later call with the same ``order_by``).
    end_cursor: str | None = None


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

def _page_info(connection: object) -> dict:
    page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
    return page_info if isinstance(page_info, dict) else {}


def _node_id(node: dict):
    return node.get("id")


def _file_key(node: dict):
    # PullRequestChangedFile has no id; the path is unique within a pull request.
    return node.get("path")


def _commit_key(node: dict):
    commit = node.get("commit")
    oid = commit.get("oid") if isinstance(commit, dict) else None
    return node.get("id") or oid


def _merge_page(connection: dict, page: dict, key=_node_id) -> None:
    """Append a follow-up page's nodes (skipping repeated keys) and adopt its pageInfo."""
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        nodes = []
        connection["nodes"] = nodes
    seen = {key(n) for n in nodes if isinstance(n, dict) and key(n)}
    for node in page.get("nodes") or []:
        if isinstance(node, dict) and key(node) and key(node) in seen:
            continue
        nodes.append(node)
        if isinstance(node, dict) and key(node):
            seen.add(key(node))
    connection["pageInfo"] = _page_info(page) or {"hasNextPage": False}
    if "totalCount" in page:
        connection["totalCount"] = page["totalCount"]


async def _page_nested_connection(
    connection: object,
    query: str,
    node_id: str,
    field: str,
    max_extra_pages: int,
    github_token: str | None,
    key=_node_id,
) -> None:
    """Follow ``connection``'s endCursor via ``node(id:)`` until done or capped.

    Each extra page is one ``run_query`` call. On a cap or an unexpected shape
    the connection keeps ``hasNextPage: True`` so the transform discloses it.
    """
    if not isinstance(connection, dict) or not node_id:
        return
    for _ in range(max_extra_pages):
        page_info = _page_info(connection)
        cursor = page_info.get("endCursor")
        if not page_info.get("hasNextPage") or not cursor:
            return
        data = await run_query(query, {"id": node_id, "cursor": cursor}, github_token=github_token)
        node = (data.get("data") or {}).get("node") if isinstance(data, dict) else None
        page = node.get(field) if isinstance(node, dict) else None
        if not isinstance(page, dict):
            return  # node vanished / wrong type: leave the disclosure in place
        _merge_page(connection, page, key)


async def _complete_nested(raw_prs: list[dict], github_token: str | None) -> None:
    """Page files, commits, reviews, review threads and thread comments, in place."""
    for raw_pr in raw_prs:
        if not isinstance(raw_pr, dict):
            continue
        pr_id = raw_pr.get("id") or ""
        await _page_nested_connection(
            raw_pr.get("files"), PR_FILES_QUERY, pr_id,
            "files", MAX_EXTRA_FILE_PAGES, github_token, key=_file_key,
        )
        await _page_nested_connection(
            raw_pr.get("commits"), PR_COMMITS_QUERY, pr_id,
            "commits", MAX_EXTRA_COMMIT_PAGES, github_token, key=_commit_key,
        )
        await _page_nested_connection(
            raw_pr.get("reviews"), PR_REVIEWS_QUERY, pr_id,
            "reviews", MAX_EXTRA_REVIEW_PAGES, github_token,
        )
        threads_conn = raw_pr.get("reviewThreads")
        await _page_nested_connection(
            threads_conn, PR_REVIEW_THREADS_QUERY, raw_pr.get("id") or "",
            "reviewThreads", MAX_EXTRA_REVIEW_THREAD_PAGES, github_token,
        )
        nodes = threads_conn.get("nodes") if isinstance(threads_conn, dict) else None
        for thread in nodes or []:
            if not isinstance(thread, dict):
                continue
            await _page_nested_connection(
                thread.get("comments"), THREAD_COMMENTS_QUERY, thread.get("id") or "",
                "comments", MAX_EXTRA_THREAD_COMMENT_PAGES, github_token,
            )


async def fetch_prs(
    owner: str, 
    repo: str, 
    pages: int = 2, 
    github_token: str | None = None,
    since_pr_number: int | None = None,
    since_updated_at: str | None = None,
    after_cursor: str | None = None,
    return_result: bool = False,
    order_by: str = "UPDATED_AT",
) -> list[dict] | FetchResult:
    """
    Fetch up to pages*30 PRs newest-first by GitHub's ``updatedAt`` (default)
    or ``createdAt`` (``order_by="CREATED_AT"``).

    ``since_updated_at`` is the preferred incremental watermark. A short overlap
    window intentionally refetches recently updated PRs; stable document IDs make
    the resulting upserts safe and prevent records updated at the boundary from
    being missed. If a bounded incremental pass hits the page cap,
    ``return_result=True`` exposes its next cursor so the caller can resume
    before committing the watermark. ``since_pr_number`` remains as a
    compatibility fallback for existing callers and old cursor databases.

    ``order_by="CREATED_AT"`` is for the initial window and older-history
    backfill: created_at never changes, so cursors stay valid between runs.
    There is no since filter in that mode (``since_*`` arguments are ignored);
    pages walk from the newest-created PR backwards. ``end_cursor`` on the
    result is usable as ``after_cursor`` for a later call.

    Files, commits, reviews, review threads and thread comments that overflow
    their first GraphQL page are completed with follow-up ``node(id:)`` queries,
    capped at ``MAX_EXTRA_*_PAGES`` (10) extra pages per connection; whatever
    remains is disclosed on the record (thread_incomplete and
    ``truncated_connections``).
    """
    if order_by not in PR_ORDER_FIELDS:
        raise ValueError(f"order_by must be one of {PR_ORDER_FIELDS}, got {order_by!r}.")
    if pages < 1:
        raise ValueError("pages must be at least 1.")
    if pages > MAX_PAGES:
        print(f"[*] pages capped at {MAX_PAGES} (requested {pages}).", file=sys.stderr)
        pages = MAX_PAGES

    created_order = order_by == "CREATED_AT"
    if created_order:
        since_updated_at = None
        since_pr_number = None
    query = _QUERIES_BY_ORDER[order_by]

    all_prs = []
    cursor = after_cursor
    watermark = _parse_github_timestamp(since_updated_at) if since_updated_at else None
    overlap_cutoff = watermark - REFRESH_OVERLAP if watermark else None
    complete = False
    next_cursor = None
    end_cursor = after_cursor

    for page_num in range(1, pages + 1):
        variables = {"owner": owner, "repo": repo}
        if cursor:
            variables["cursor"] = cursor

        print(f"[*] Fetching page {page_num}/{pages} for {owner}/{repo}...", file=sys.stderr)
        data = await run_query(query, variables, github_token=github_token)
        repository = data.get("data", {}).get("repository")
        if not repository:
            raise ValueError(f"Repository '{owner}/{repo}' not found or inaccessible.")
        pr_data = repository.get("pullRequests")
        if not pr_data:
            raise ValueError(f"GitHub returned no pull-request connection for '{owner}/{repo}'.")

        raw_nodes = pr_data.get("nodes") or []
        for node in raw_nodes:
            if not isinstance(node, dict):
                raise ValueError("GitHub returned a pull request node that is not an object.")
        page_info = pr_data["pageInfo"]
        end_cursor = page_info.get("endCursor") or end_cursor
        reached_boundary = False

        # UPDATED_AT pagination is newest-first. Keep a small overlap so an
        # update occurring while a previous sync was finishing cannot be lost.
        # Filter before the (extra-query) nested paging so dropped PRs cost nothing.
        if overlap_cutoff:
            updated = [
                _parse_github_timestamp(n.get("updatedAt") or n.get("createdAt") or "")
                for n in raw_nodes
            ]
            keep = [n for n, ts in zip(raw_nodes, updated) if ts >= overlap_cutoff]
            reached_boundary = len(keep) < len(raw_nodes) and any(ts < overlap_cutoff for ts in updated)
            if reached_boundary:
                print(f"[+] Reached refresh watermark ({since_updated_at}). Stopping.", file=sys.stderr)
        elif since_pr_number:
            keep = [n for n in raw_nodes if n["number"] > since_pr_number]
            reached_boundary = len(keep) < len(raw_nodes)
            if reached_boundary:
                print(f"[+] Reached already indexed PRs (>{since_pr_number}). Stopping.", file=sys.stderr)
        else:
            keep = raw_nodes

        await _complete_nested(keep, github_token)
        all_prs.extend(flatten_prs(keep, observed_at=now_utc_iso()))

        if reached_boundary:
            complete = True
            break

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

    result = FetchResult(
        prs=all_prs, complete=complete, next_cursor=next_cursor, end_cursor=end_cursor
    )
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
