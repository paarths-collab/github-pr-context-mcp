import sys
import asyncio
import json
from datetime import datetime, timezone
from mcp.server.fastmcp import Context
from auth import GitHubAuthorizationError
from app.index_status import (
    _INDEX_JOBS,
    _INDEX_JOBS_LOCK,
    describe_index_state,
    error_details,
    format_index_status,
    get_index_job,
    job_key as _job_key,
)
from app.state import is_temporary
from fetcher import fetch_prs
from storage import (
    IndexInvalidatedError,
    checkpoint_refresh,
    complete_refresh,
    compute_coverage,
    delete_repo_index as delete_repo_index_storage,
    get_collection_stats,
    get_index_epoch,
    get_index_registry,
    index_prs,
    get_refresh_state,
    list_all_repos,
    record_job_outcome,
    repo_is_indexed_permanently,
    repo_is_indexed_temporarily,
    mark_repo_checked,
    set_backfill_state,
)

# The job map itself lives in app.index_status so retrieval tools can describe
# index state without importing this module (and the fetcher/Chroma with it).
_INDEX_TASKS: dict[str, asyncio.Task] = {}

PAGE_SIZE = 30  # pull requests per GitHub page (used to estimate pages consumed)


def _set_index_job(job_key: str, **values) -> dict:
    """Atomically record a user-visible asynchronous indexing state."""
    with _INDEX_JOBS_LOCK:
        current = dict(_INDEX_JOBS.get(job_key, {}))
        current.update(values)
        _INDEX_JOBS[job_key] = current
        return dict(current)


def _claim_index_job(job_key: str, job: dict) -> tuple[bool, dict]:
    """Atomically reserve an index job so duplicate callers cannot both start it."""
    with _INDEX_JOBS_LOCK:
        existing = _INDEX_JOBS.get(job_key)
        if existing and existing.get("status") in {"queued", "running"}:
            return False, dict(existing)
        _INDEX_JOBS[job_key] = dict(job)
        return True, dict(job)


def _record_failed_job_if_idle(job_key: str, job: dict) -> tuple[bool, dict]:
    """Record a pre-claim failure unless a job is already queued/running.

    A credential failure that happens while another job owns the scope must not
    overwrite that job's record (it would hide the running job and let a second
    caller claim the scope concurrently).
    """
    with _INDEX_JOBS_LOCK:
        existing = _INDEX_JOBS.get(job_key)
        if existing and existing.get("status") in {"queued", "running"}:
            return False, dict(existing)
        _INDEX_JOBS[job_key] = dict(job)
        return True, dict(job)


def _retain_index_task(job_key: str, task: asyncio.Task) -> None:
    """Keep a strong reference to the background task until it finishes."""
    with _INDEX_JOBS_LOCK:
        _INDEX_TASKS[job_key] = task

    def _cleanup(done_task: asyncio.Task) -> None:
        with _INDEX_JOBS_LOCK:
            if _INDEX_TASKS.get(job_key) is done_task:
                _INDEX_TASKS.pop(job_key, None)
            current = _INDEX_JOBS.get(job_key)
            if done_task.cancelled() and current and current.get("status") in {"queued", "running"}:
                current.update({
                    "status": "failed",
                    "error": "Indexing task was cancelled.",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })

    task.add_done_callback(_cleanup)


def _get_index_job(repo_key: str, namespace: str | None) -> dict | None:
    """Return the newest job for a repository/namespace across both stores."""
    return get_index_job(repo_key, namespace)


def _invalidate_index_job(repo_key: str, namespace: str | None, temporary: bool) -> None:
    """Cancel a visible job; the storage epoch prevents late thread writes."""
    job_key = _job_key(repo_key, namespace, temporary)
    task: asyncio.Task | None = None
    with _INDEX_JOBS_LOCK:
        job = _INDEX_JOBS.get(job_key)
        if job:
            job.update(
                {
                    "status": "cancelled",
                    "error": "Indexing was cancelled because this index was deleted.",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        task = _INDEX_TASKS.get(job_key)
    if task and not task.done():
        task.cancel()
from app.state import (
    normalize_repo,
    normalize_namespace,
    resolve_namespace,
    repo_state_key,
    get_state,
    namespace_text,
    track_usage,
    resolve_repo,
    get_github_access_token,
)

STORAGE_CONSEQUENCES = """
Permanent storage
  - PR data is embedded and saved to disk (ChromaDB).
  - Available instantly on future sessions.
  - Disk usage: ~5-20 MB per repo (60 PRs).
  - Best for repos you query repeatedly.

Temporary storage
  - PR data is embedded and kept in memory only.
  - Faster to set up, zero disk usage.
  - Lost when the MCP server restarts.
  - Best for one-off exploration.
"""

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _storage_name(temporary: bool) -> str:
    return "temporary" if temporary else "permanent"


def _persist_job_outcome(
    repo_key: str,
    namespace: str | None,
    temporary: bool,
    status: str,
    error_kind: str | None = None,
    expected_epoch: int | None = None,
    *,
    strict: bool = False,
    kind: str | None = None,
) -> None:
    """Record the last job outcome durably (survives restart).

    ``kind`` ("initial"/"refresh"/"backfill") lets the registry track refresh
    health separately from the last job.

    ``strict`` lets an ``IndexInvalidatedError`` (the index was deleted while
    the job ran) propagate so the caller reports cancellation; failure paths
    use best effort because they must not mask the original error.
    """
    try:
        record_job_outcome(
            repo_key,
            namespace,
            _storage_name(temporary),
            status=status,
            error_kind=error_kind,
            finished_at=_now_iso(),
            expected_epoch=expected_epoch,
            kind=kind,
        )
    except IndexInvalidatedError:
        if strict:
            raise
    except Exception as exc:  # pragma: no cover - best effort bookkeeping
        if strict:
            raise
        print(f"[!] Could not persist job outcome for {repo_key}: {exc}", file=sys.stderr)


def _safe_coverage(repo_key: str, namespace: str | None, temporary: bool) -> dict | None:
    try:
        coverage = compute_coverage(repo_key, namespace, _storage_name(temporary))
    except Exception as exc:  # status text must never fail a finished job
        print(f"[!] Could not compute coverage for {repo_key}: {exc}", file=sys.stderr)
        return None
    return coverage if isinstance(coverage, dict) else None


def _pr_span(coverage: dict | None, prs: list[dict]) -> tuple[int | None, int | None, int | None]:
    """(count, min, max) of indexed PR numbers; falls back to this fetch."""
    if coverage and coverage.get("pull_requests_indexed"):
        return (
            coverage["pull_requests_indexed"],
            coverage.get("pr_number_min"),
            coverage.get("pr_number_max"),
        )
    numbers = [pr["number"] for pr in prs if isinstance(pr.get("number"), int)]
    if not numbers:
        return None, None, None
    return len(numbers), min(numbers), max(numbers)


def _history_message(
    repo_key: str,
    kind: str,
    coverage: dict | None,
    prs: list[dict],
    history_complete: bool,
) -> str:
    """Human-readable result that never implies full history when it is not."""
    count, low, high = _pr_span(coverage, prs)
    span = f" (#{low}-#{high})" if low is not None and high is not None else ""
    noun = "PR" if count == 1 else "PRs"
    lead = f"{repo_key}: indexed {count if count is not None else 0} {noun}{span}"
    if history_complete:
        text = f"{lead}; the full closed pull request history is imported."
    else:
        text = (
            f"{lead}; older history not imported. This is a bounded window, not "
            "full repository history. Run ensure_repo_ready with backfill=true "
            "to continue importing older pull requests."
        )
    if kind == "backfill":
        text = f"Backfilled {len(prs)} PRs. " + text
    return text


def _pages_consumed(pages: int, pr_count: int, complete: bool) -> int:
    if not complete:
        return pages
    return max(1, min(pages, -(-pr_count // PAGE_SIZE)))


def _persisted_cursor(fetched, complete: bool) -> str:
    """Cursor to resume older history from ('' once history is complete)."""
    if complete:
        return ""
    return getattr(fetched, "end_cursor", None) or getattr(fetched, "next_cursor", None) or ""


def _describe_for_message(repo_key: str, namespace: str | None, temporary: bool) -> str:
    """Index status lines for short-circuit responses (never raises)."""
    try:
        return "\n" + format_index_status(
            describe_index_state(repo_key, namespace, _storage_name(temporary))
        )
    except Exception as exc:
        print(f"[!] Could not describe index state for {repo_key}: {exc}", file=sys.stderr)
        return ""


def register_indexing_tools(mcp):
    @mcp.tool(name="ensure_repo_ready")
    async def ensure_repo_ready(
        repo: str | None = None,
        storage: str | None = None,
        pages: int = 2,
        refresh: bool = False,
        namespace: str | None = None,
        backfill: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Index a repository, refresh it, or continue importing older history.

        The first import fetches the newest `pages` pages of closed PRs ordered
        by creation date (a bounded window, not full history). `refresh=true`
        pulls PRs updated since the last refresh. `backfill=true` continues
        older history from the persisted cursor for `pages` pages and works
        after a restart.
        """
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        # Handle case where repo is missing by trying to detect it from CWD
        try:
            repo_key = await asyncio.to_thread(resolve_repo, repo, state)
        except ValueError as e:
            return str(e)

        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "ensure_repo_ready")
        state_key = repo_state_key(repo_key, namespace)

        if pages < 1 or pages > 10:
            raise ValueError("pages must be between 1 and 10.")
        if storage not in {None, "temporary", "permanent"}:
            raise ValueError("storage must be one of: temporary, permanent")
        if refresh and backfill:
            raise ValueError(
                "refresh and backfill are separate operations; call ensure_repo_ready "
                "with only one of them."
            )

        permanently_indexed = await asyncio.to_thread(
            repo_is_indexed_permanently, repo_key, namespace=namespace
        )
        temporarily_indexed = await asyncio.to_thread(
            repo_is_indexed_temporarily, repo_key, namespace=namespace
        )

        if permanently_indexed and not refresh and not backfill and storage in {None, "permanent"}:
            state["active_repo"] = repo_key
            state["active_namespace"] = namespace
            state["storage_types"][state_key] = "permanent"
            stats = await asyncio.to_thread(get_collection_stats, repo_key, temporary=False, namespace=namespace)
            status_text = await asyncio.to_thread(_describe_for_message, repo_key, namespace, False)
            return (
                f"{repo_key} is already indexed permanently on disk.\n"
                f"{stats['total_documents']} documents loaded and ready.\n"
                f"Active repo set to {repo_key}."
                f"{status_text}"
                f"{namespace_text(namespace)}"
            )

        if temporarily_indexed and not refresh and not backfill and storage in {None, "temporary"}:
            state["active_repo"] = repo_key
            state["active_namespace"] = namespace
            state["storage_types"][state_key] = "temporary"
            stats = await asyncio.to_thread(get_collection_stats, repo_key, temporary=True, namespace=namespace)
            status_text = await asyncio.to_thread(_describe_for_message, repo_key, namespace, True)
            return (
                f"{repo_key} is already indexed in memory.\n"
                f"{stats['total_documents']} documents loaded and ready.\n"
                f"Active repo set to {repo_key}."
                f"{status_text}"
                f"{namespace_text(namespace)}"
            )

        # A refresh or backfill inherits the existing storage choice. First
        # indexing still asks the caller to make the persistence decision.
        if (refresh or backfill) and storage is None:
            if permanently_indexed:
                storage = "permanent"
            elif temporarily_indexed:
                storage = "temporary"

        if storage is None:
            return (
                f"{repo_key} is not indexed yet."
                f"{namespace_text(namespace)}\n\n"
                f"How would you like to store it?\n\n"
                f"{STORAGE_CONSEQUENCES}\n"
                f"Reply with permanent or temporary and I will fetch/index up to {pages * 30} PRs "
                f"(the newest by creation date; older history can be added later with backfill=true)."
            )

        temporary = storage == "temporary"
        target_was_indexed = temporarily_indexed if temporary else permanently_indexed

        if backfill:
            if not target_was_indexed:
                return (
                    f"{repo_key} is not indexed in {storage} storage, so there is no "
                    "history to continue. Run ensure_repo_ready first to import the "
                    f"initial window.{namespace_text(namespace)}"
                )
            registry = await asyncio.to_thread(
                get_index_registry, repo_key, namespace, _storage_name(temporary)
            )
            if registry.get("history_complete") is True and not registry.get("backfill_cursor"):
                coverage = await asyncio.to_thread(_safe_coverage, repo_key, namespace, temporary)
                summary = f"\nCoverage: {coverage['summary']}" if coverage else ""
                return (
                    f"Nothing to backfill: the full closed pull request history of "
                    f"{repo_key} is already indexed.{summary}{namespace_text(namespace)}"
                )

        if backfill:
            kind = "backfill"
        elif refresh and target_was_indexed:
            kind = "refresh"
        else:
            kind = "initial"
        job_key = _job_key(repo_key, namespace, temporary)

        # Resolve the credential before claiming a background job. The job then
        # owns only a short-lived local value, never a request context or a
        # direct environment lookup.
        try:
            github_token = await get_github_access_token()
        except GitHubAuthorizationError as exc:
            details = error_details(exc)
            now = _now_iso()
            recorded, existing_job = _record_failed_job_if_idle(
                job_key,
                {
                    "status": "failed",
                    "kind": kind,
                    "repo": repo_key,
                    "namespace": namespace,
                    "storage": storage,
                    "temporary": temporary,
                    "refresh": refresh,
                    "backfill": backfill,
                    "started_at": now,
                    "finished_at": now,
                    "error": f"{type(exc).__name__}: {exc}",
                    "message": None,
                    **details,
                },
            )
            if not recorded:
                # Another job owns this scope: leave its record and the durable
                # outcome alone; this call changes nothing.
                return (
                    f"Indexing is already {existing_job['status']} for {repo_key} "
                    f"[{storage}]. Use get_index_stats to monitor it. (This call "
                    "could not obtain a GitHub credential and made no change.)"
                    f"{namespace_text(namespace)}"
                )
            await asyncio.to_thread(
                _persist_job_outcome, repo_key, namespace, temporary, "failed",
                details["error_kind"], kind=kind,
            )
            return (
                f"GitHub connection is required before indexing. {exc}\n"
                f"error_kind: {details['error_kind']}\n"
                f"Action: {details['action']}"
                f"{namespace_text(namespace)}"
            )

        # Capture the write epoch before claiming the job. A concurrent delete
        # after this point invalidates the captured epoch, so this job cannot
        # recreate an index the user explicitly removed.
        expected_epoch = await asyncio.to_thread(
            get_index_epoch,
            repo_key,
            temporary=temporary,
            namespace=namespace,
        )
        started_at = _now_iso()
        job = {
            "status": "queued",
            "kind": kind,
            "repo": repo_key,
            "namespace": namespace,
            "storage": storage,
            "temporary": temporary,
            "refresh": refresh,
            "backfill": backfill,
            "pages": pages,
            "started_at": started_at,
            "finished_at": None,
            "documents_indexed": None,
            "truncated_connections": [],
            "error": None,
            "error_kind": None,
            "action": None,
            "message": None,
            "history_complete": None,
            "older_history_remaining": None,
            "expected_epoch": expected_epoch,
        }
        claimed, existing_job = _claim_index_job(job_key, job)
        if not claimed:
            return (
                f"Indexing is already {existing_job['status']} for {repo_key} "
                f"[{storage}]. Use get_index_stats to monitor it."
                f"{namespace_text(namespace)}"
            )

        async def _finish(
            status: str,
            *,
            count: int,
            message: str,
            history_complete: bool | None = None,
            truncated_connections: list[str] | None = None,
        ) -> None:
            """Persist the outcome (epoch protected), then publish the job result."""
            await asyncio.to_thread(
                _persist_job_outcome, repo_key, namespace, temporary, status, None,
                expected_epoch, strict=True, kind=kind,
            )
            with _INDEX_JOBS_LOCK:
                cancelled = (_INDEX_JOBS.get(job_key) or {}).get("status") == "cancelled"
            if cancelled:  # deleted while running: never overwrite the cancellation
                return
            _set_index_job(
                job_key,
                status=status,
                documents_indexed=count,
                truncated_connections=truncated_connections or [],
                message=message,
                history_complete=history_complete,
                older_history_remaining=(
                    None if history_complete is None else not history_complete
                ),
                finished_at=_now_iso(),
            )

        def _activate() -> None:
            state["active_repo"] = repo_key
            state["active_namespace"] = namespace
            state["storage_types"][state_key] = storage

        async def _run_indexing():
            try:
                _set_index_job(job_key, status="running")
                refresh_state = await asyncio.to_thread(
                    get_refresh_state,
                    repo_key,
                    namespace=namespace,
                    temporary=temporary,
                )
                fetch_kwargs: dict = {}
                since_updated_at = None
                after_cursor = None
                is_incremental_refresh = False
                if kind == "backfill":
                    registry = await asyncio.to_thread(
                        get_index_registry, repo_key, namespace, _storage_name(temporary)
                    )
                    after_cursor = registry.get("backfill_cursor") or None
                    fetch_kwargs["order_by"] = "CREATED_AT"
                    msg = (
                        "older-history backfill resuming from the saved cursor"
                        if after_cursor
                        else "older-history backfill from the newest created PR (no saved cursor)"
                    )
                elif kind == "refresh":
                    since_updated_at = refresh_state["last_updated_at"]
                    is_incremental_refresh = since_updated_at is not None
                    after_cursor = (
                        refresh_state["refresh_cursor"] if since_updated_at else None
                    )
                    msg = (
                        f"incremental update since {since_updated_at}"
                        if since_updated_at
                        else "full refresh (no watermark yet)"
                    )
                    if after_cursor:
                        msg += " (resuming a capped refresh)"
                else:
                    fetch_kwargs["order_by"] = "CREATED_AT"
                    msg = "initial import (newest created PRs first)"
                print(f"[*] Starting background indexing for {repo_key} ({msg})...", file=sys.stderr)

                fetched = await fetch_prs(
                    *repo_key.split("/", 1),
                    pages=pages,
                    github_token=github_token,
                    since_updated_at=since_updated_at,
                    after_cursor=after_cursor,
                    return_result=True,
                    **fetch_kwargs,
                )
                if isinstance(fetched, list):  # Compatibility with in-process extensions/tests.
                    prs = fetched
                    fetch_complete = True
                    next_cursor = None
                else:
                    prs = fetched.prs
                    fetch_complete = fetched.complete
                    next_cursor = fetched.next_cursor
                if is_incremental_refresh and not fetch_complete and not next_cursor:
                    raise RuntimeError(
                        "GitHub returned a capped refresh without a continuation cursor."
                    )

                truncated_connections = sorted({
                    connection
                    for pr in prs
                    for connection in (
                        pr.get("truncated_connections", [])
                        if isinstance(pr.get("truncated_connections", []), list)
                        else []
                    )
                    if isinstance(connection, str)
                })

                # ── Older-history import: initial window or backfill ──────────
                if kind in {"initial", "backfill"}:
                    count = 0
                    if prs:
                        count = await asyncio.to_thread(
                            index_prs,
                            repo_key,
                            prs,
                            temporary=temporary,
                            namespace=namespace,
                            # An initial window establishes the refresh watermark;
                            # a backfill must never move it.
                            advance_watermark=(kind == "initial"),
                            expected_epoch=expected_epoch,
                            is_refresh=False,
                        )
                    elif kind == "initial":
                        # Only the initial import syncs with GitHub's newest
                        # state; an empty backfill says nothing about freshness
                        # and must not reset the stale clock.
                        await asyncio.to_thread(
                            mark_repo_checked,
                            repo_key,
                            temporary=temporary,
                            namespace=namespace,
                            expected_epoch=expected_epoch,
                        )
                    # Persist the resume point only after the generation is
                    # published: a failure above keeps the last good cursor.
                    cursor_to_save = _persisted_cursor(fetched, fetch_complete)
                    backfill_fields: dict = {
                        "backfill_cursor": cursor_to_save,
                        "history_complete": True if fetch_complete else False,
                        "expected_epoch": expected_epoch,
                    }
                    pages_used = _pages_consumed(pages, len(prs), fetch_complete)
                    if kind == "initial":
                        backfill_fields["pages_done"] = pages_used
                    else:
                        backfill_fields["add_pages"] = pages_used
                    if prs or fetch_complete:
                        await asyncio.to_thread(
                            set_backfill_state,
                            repo_key,
                            namespace,
                            _storage_name(temporary),
                            **backfill_fields,
                        )
                    _activate()
                    coverage = await asyncio.to_thread(
                        _safe_coverage, repo_key, namespace, temporary
                    )
                    if not prs:
                        message = (
                            f"No closed pull requests were found for {repo_key}; "
                            "nothing was indexed."
                            if kind == "initial"
                            else f"Backfill found no older pull requests for {repo_key}; "
                            "history is complete."
                        )
                    else:
                        message = _history_message(
                            repo_key, kind, coverage, prs, bool(fetch_complete)
                        )
                    # A backfill that stopped on the pages cap leaves a requested
                    # selection unfinished (ready_partial); an initial window is
                    # exactly what was requested (ready_selected_scope).
                    status = "partial" if kind == "backfill" and not fetch_complete else "ready"
                    await _finish(
                        status,
                        count=count,
                        message=message,
                        history_complete=bool(fetch_complete),
                        truncated_connections=truncated_connections,
                    )
                    print(f"[+] {message}", file=sys.stderr)
                    return

                # ── Incremental refresh (existing semantics) ──────────────────
                updated_at_values = [
                    value
                    for pr in prs
                    if isinstance((value := pr.get("updated_at")), str) and value
                ]
                high_watermark_candidates = [
                    refresh_state.get("refresh_high_watermark"),
                    *updated_at_values,
                ]
                high_watermark = max(
                    (value for value in high_watermark_candidates if value),
                    default=None,
                )

                if not prs:
                    await asyncio.to_thread(
                        mark_repo_checked,
                        repo_key,
                        temporary=temporary,
                        namespace=namespace,
                        expected_epoch=expected_epoch,
                    )
                    if is_incremental_refresh and fetch_complete and refresh_state.get("refresh_cursor"):
                        await asyncio.to_thread(
                            complete_refresh,
                            repo_key,
                            0,
                            temporary=temporary,
                            namespace=namespace,
                            high_watermark=high_watermark,
                            expected_epoch=expected_epoch,
                        )
                    elif is_incremental_refresh and not fetch_complete:
                        await asyncio.to_thread(
                            checkpoint_refresh,
                            repo_key,
                            temporary=temporary,
                            namespace=namespace,
                            next_cursor=next_cursor,
                            high_watermark=high_watermark,
                            expected_epoch=expected_epoch,
                        )
                        await _finish(
                            "partial",
                            count=0,
                            message=(
                                "Refresh reached the page cap. Run ensure_repo_ready "
                                "with refresh=true again to continue."
                            ),
                        )
                        return
                    print(f"[+] No new PRs found for {repo_key}. Already up to date.", file=sys.stderr)
                    _activate()
                    await _finish("ready", count=0, message="Already up to date; no new pull requests.")
                    return

                count = await asyncio.to_thread(
                    index_prs,
                    repo_key,
                    prs,
                    temporary=temporary,
                    namespace=namespace,
                    advance_watermark=not (is_incremental_refresh and not fetch_complete),
                    expected_epoch=expected_epoch,
                    is_refresh=True,
                )
                if is_incremental_refresh and not fetch_complete:
                    await asyncio.to_thread(
                        checkpoint_refresh,
                        repo_key,
                        temporary=temporary,
                        namespace=namespace,
                        next_cursor=next_cursor,
                        high_watermark=high_watermark,
                        expected_epoch=expected_epoch,
                    )
                    await _finish(
                        "partial",
                        count=count,
                        message=(
                            "Refresh reached the page cap. Run ensure_repo_ready "
                            "with refresh=true again to continue."
                        ),
                    )
                    print(
                        f"[*] Refresh paused for {repo_key}; continuation is required before its watermark advances.",
                        file=sys.stderr,
                    )
                    return

                if is_incremental_refresh and refresh_state.get("refresh_cursor"):
                    max_pr_number = max(
                        (
                            pr.get("number", 0)
                            for pr in prs
                            if isinstance(pr.get("number", 0), int)
                        ),
                        default=0,
                    )
                    await asyncio.to_thread(
                        complete_refresh,
                        repo_key,
                        max_pr_number,
                        temporary=temporary,
                        namespace=namespace,
                        high_watermark=high_watermark,
                        expected_epoch=expected_epoch,
                    )
                _activate()
                await _finish(
                    "ready",
                    count=count,
                    message=f"Refreshed {repo_key}: {len(prs)} pull requests re-indexed.",
                    truncated_connections=truncated_connections,
                )
                print(f"[+] Background indexing finished for {repo_key}. {count} docs parsed.", file=sys.stderr)
            except IndexInvalidatedError:
                _set_index_job(
                    job_key,
                    status="cancelled",
                    error="Indexing was cancelled because this index was deleted.",
                    finished_at=_now_iso(),
                )
                print(f"[*] Background indexing cancelled for {repo_key} after deletion.", file=sys.stderr)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                details = error_details(e)
                # Visible first; then durable so the failure survives a restart.
                # Any generation published by an earlier job keeps serving.
                _set_index_job(
                    job_key,
                    status="failed",
                    error=error,
                    finished_at=_now_iso(),
                    **details,
                )
                await asyncio.to_thread(
                    _persist_job_outcome, repo_key, namespace, temporary, "failed",
                    details["error_kind"], expected_epoch, kind=kind,
                )
                print(f"[!] Background indexing failed for {repo_key}: {error}", file=sys.stderr)

        task = asyncio.create_task(_run_indexing())
        _retain_index_task(job_key, task)

        storage_label = "temporary (in-memory)" if temporary else "permanent (disk)"
        if kind == "backfill":
            action = "Backfill of older history"
        elif kind == "refresh":
            action = "Refresh"
        else:
            action = "Background indexing"
        scope_note = ""
        if kind == "initial":
            scope_note = (
                f"\nThis imports the newest {pages * PAGE_SIZE} closed PRs by creation date "
                "(a bounded window); use backfill=true later for older history."
            )
        return (
            f"{action} started for {repo_key} [{storage_label}].\n"
            f"Use the 'get_index_stats' tool to verify its status, document count, and any error."
            f"{scope_note}\n"
            f"Active repo will be activated upon completion."
            f"{namespace_text(namespace)}"
        )

    @mcp.tool(name="set_active_repo")
    async def set_active_repo(repo: str, namespace: str | None = None, ctx: Context | None = None) -> str:
        """Switch the active repo to an already-indexed repo."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        repo_key = normalize_repo(repo)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "set_active_repo")

        if not await asyncio.to_thread(repo_is_indexed_permanently, repo_key, namespace=namespace) and not await asyncio.to_thread(repo_is_indexed_temporarily, repo_key, namespace=namespace):
            return f"{repo_key} is not indexed yet. Use ensure_repo_ready first."

        state_key = repo_state_key(repo_key, namespace)
        if await asyncio.to_thread(repo_is_indexed_temporarily, repo_key, namespace=namespace):
            state["storage_types"][state_key] = "temporary"
        else:
            state["storage_types"][state_key] = "permanent"

        previous = state.get("active_repo")
        state["active_repo"] = repo_key
        state["active_namespace"] = namespace

        msg = f"Active repo switched to: {repo_key}"
        if previous and previous != repo_key:
            msg += f"\n(previously: {previous})"
        if namespace:
            msg += f"\n(namespace: {namespace})"
        return msg

    @mcp.tool(name="list_indexed_repos")
    async def list_indexed_repos(namespace: str | None = None, ctx: Context | None = None) -> str:
        """List indexed repos with storage type and document count."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "list_indexed_repos")
        rows = await asyncio.to_thread(list_all_repos, namespace=namespace)
        if not rows:
            return "No repos indexed yet."

        active_repo = state.get("active_repo")
        active_ns = state.get("active_namespace")

        lines = ["Indexed repos:"]
        for r in rows:
            icon = "disk" if r["storage"] == "permanent" else "mem"
            repo_ns = normalize_namespace(r.get("namespace"))
            marker = " <- active" if r["repo"] == active_repo and repo_ns == active_ns else ""
            ns_label = repo_ns or "default"
            lines.append(
                f"- {icon} {r['repo']} ({r['total_documents']} docs, {r['storage']}, ns={ns_label}){marker}"
            )

        return "\n".join(lines)

    @mcp.tool(name="delete_repo_index")
    async def delete_repo_index(
        repo: str,
        storage: str = "both",
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Delete an indexed repo from temporary, permanent, or both storage scopes."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        repo_key = normalize_repo(repo)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "delete_repo_index")

        if storage in {"temporary", "both"}:
            _invalidate_index_job(repo_key, namespace, temporary=True)
        if storage in {"permanent", "both"}:
            _invalidate_index_job(repo_key, namespace, temporary=False)
        result = await asyncio.to_thread(delete_repo_index_storage, repo_key, storage=storage, namespace=namespace)
        if not result["deleted_any"]:
            return f"No index found for {repo_key}{namespace_text(namespace)} in storage scope: {storage}."

        deleted_labels = []
        if result["deleted"]["temporary"]:
            deleted_labels.append("temporary")
        if result["deleted"]["permanent"]:
            deleted_labels.append("permanent")

        state_key = repo_state_key(repo_key, namespace)
        if storage in {"both", state["storage_types"].get(state_key)}:
            state["storage_types"].pop(state_key, None)

        if state.get("active_repo") == repo_key and normalize_namespace(state.get("active_namespace")) == namespace:
            if storage == "both":
                state["active_repo"] = None
                state["active_namespace"] = None

        return (
            f"Deleted index for {repo_key} from: {', '.join(deleted_labels)}."
            f"{namespace_text(namespace)}"
        )

    @mcp.tool(name="get_index_stats")
    async def get_index_stats(
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Return indexed document count and storage scope for the selected repo."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "get_index_stats")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        job = _get_index_job(repo_key, namespace)
        temporary = is_temporary(repo_key, namespace, state)
        if job and job.get("status") in {"queued", "running", "failed", "partial", "cancelled"}:
            temporary = bool(job.get("temporary"))

        stats = await asyncio.to_thread(get_collection_stats, repo_key, temporary=temporary, namespace=namespace)
        description = await asyncio.to_thread(
            describe_index_state, repo_key, namespace, _storage_name(temporary)
        )
        stats["index_job"] = job
        stats["index_state"] = description["index_state"]
        stats["stale_reason"] = description["stale_reason"]
        stats["coverage"] = description["coverage"]
        stats["generation"] = description["generation"]
        stats["serving_generation"] = description["serving_generation"]
        stats["last_job"] = description["last_job"]
        stats["action"] = description["action"]
        job_message = (job or {}).get("message") if job and job.get("temporary") == temporary else None
        stats["message"] = job_message or description["coverage"].get("summary")
        return json.dumps(stats, indent=2)
