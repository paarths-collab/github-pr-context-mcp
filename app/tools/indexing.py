import sys
import asyncio
import threading
import json
from datetime import datetime, timezone
from mcp.server.fastmcp import Context
from auth import GitHubAuthorizationError
from app.state import is_temporary
from fetcher import fetch_prs
from storage import (
    IndexInvalidatedError,
    checkpoint_refresh,
    complete_refresh,
    delete_repo_index as delete_repo_index_storage,
    get_collection_stats,
    get_index_epoch,
    index_prs,
    get_refresh_state,
    list_all_repos,
    repo_is_indexed_permanently,
    repo_is_indexed_temporarily,
    mark_repo_checked,
)


_INDEX_JOBS: dict[str, dict] = {}
_INDEX_JOBS_LOCK = threading.Lock()
_INDEX_TASKS: dict[str, asyncio.Task] = {}


def _namespace_scope(namespace: str | None) -> str:
    normalized = namespace.strip() if isinstance(namespace, str) else ""
    return "default:" if not normalized else f"namespace:{normalized}"


def _job_key(repo_key: str, namespace: str | None, temporary: bool) -> str:
    storage = "temporary" if temporary else "permanent"
    return f"{repo_key}\0{_namespace_scope(namespace)}\0{storage}"


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
    prefix = f"{repo_key}\0{_namespace_scope(namespace)}\0"
    with _INDEX_JOBS_LOCK:
        matches = [job for key, job in _INDEX_JOBS.items() if key.startswith(prefix)]
        if not matches:
            return None
        return dict(max(matches, key=lambda job: job.get("started_at", "")))


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

def register_indexing_tools(mcp):
    @mcp.tool(name="ensure_repo_ready")
    async def ensure_repo_ready(
        repo: str | None = None,
        storage: str | None = None,
        pages: int = 2,
        refresh: bool = False,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Index a repository or refresh its GitHub PR evidence asynchronously."""
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

        permanently_indexed = await asyncio.to_thread(
            repo_is_indexed_permanently, repo_key, namespace=namespace
        )
        temporarily_indexed = await asyncio.to_thread(
            repo_is_indexed_temporarily, repo_key, namespace=namespace
        )

        if permanently_indexed and not refresh and storage in {None, "permanent"}:
            state["active_repo"] = repo_key
            state["active_namespace"] = namespace
            state["storage_types"][state_key] = "permanent"
            stats = await asyncio.to_thread(get_collection_stats, repo_key, temporary=False, namespace=namespace)
            return (
                f"{repo_key} is already indexed permanently on disk.\n"
                f"{stats['total_documents']} documents loaded and ready.\n"
                f"Active repo set to {repo_key}."
                f"{namespace_text(namespace)}"
            )

        if temporarily_indexed and not refresh and storage in {None, "temporary"}:
            state["active_repo"] = repo_key
            state["active_namespace"] = namespace
            state["storage_types"][state_key] = "temporary"
            stats = await asyncio.to_thread(get_collection_stats, repo_key, temporary=True, namespace=namespace)
            return (
                f"{repo_key} is already indexed in memory.\n"
                f"{stats['total_documents']} documents loaded and ready.\n"
                f"Active repo set to {repo_key}."
                f"{namespace_text(namespace)}"
            )

        # A refresh inherits the existing storage choice. First indexing still
        # asks the caller to make the persistence decision explicitly.
        if refresh and storage is None:
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
                f"Reply with permanent or temporary and I will fetch/index up to {pages * 30} PRs."
            )

        # Resolve the credential before claiming a background job. The job then
        # owns only a short-lived local value, never a request context or a
        # direct environment lookup.
        try:
            github_token = await get_github_access_token()
        except GitHubAuthorizationError as exc:
            return f"GitHub connection is required before indexing. {exc}"

        temporary = storage == "temporary"
        target_was_indexed = temporarily_indexed if temporary else permanently_indexed
        # Capture the write epoch before claiming the job. A concurrent delete
        # after this point invalidates the captured epoch, so this job cannot
        # recreate an index the user explicitly removed.
        expected_epoch = await asyncio.to_thread(
            get_index_epoch,
            repo_key,
            temporary=temporary,
            namespace=namespace,
        )
        job_key = _job_key(repo_key, namespace, temporary)
        started_at = datetime.now(timezone.utc).isoformat()
        job = {
            "status": "queued",
            "repo": repo_key,
            "namespace": namespace,
            "storage": storage,
            "temporary": temporary,
            "refresh": refresh,
            "started_at": started_at,
            "finished_at": None,
            "documents_indexed": None,
            "truncated_connections": [],
            "error": None,
            "message": None,
            "expected_epoch": expected_epoch,
        }
        claimed, existing_job = _claim_index_job(job_key, job)
        if not claimed:
            return (
                f"Indexing is already {existing_job['status']} for {repo_key} "
                f"[{storage}]. Use get_index_stats to monitor it."
                f"{namespace_text(namespace)}"
            )

        async def _run_indexing():
            try:
                _set_index_job(job_key, status="running")
                refresh_state = await asyncio.to_thread(
                    get_refresh_state,
                    repo_key,
                    namespace=namespace,
                    temporary=temporary,
                )
                since_updated_at = (
                    refresh_state["last_updated_at"] if target_was_indexed else None
                )
                is_incremental_refresh = refresh and since_updated_at is not None
                after_cursor = (
                    refresh_state["refresh_cursor"] if since_updated_at else None
                )
                msg = (
                    f"incremental update since {since_updated_at}"
                    if since_updated_at
                    else "full index"
                )
                if after_cursor:
                    msg += " (resuming a capped refresh)"
                print(f"[*] Starting background indexing for {repo_key} ({msg})...", file=sys.stderr)

                fetched = await fetch_prs(
                    *repo_key.split("/", 1),
                    pages=pages,
                    github_token=github_token,
                    since_updated_at=since_updated_at,
                    after_cursor=after_cursor,
                    return_result=True,
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
                        _set_index_job(
                            job_key,
                            status="partial",
                            documents_indexed=0,
                            message=(
                                "Refresh reached the page cap. Run ensure_repo_ready "
                                "with refresh=true again to continue."
                            ),
                            finished_at=datetime.now(timezone.utc).isoformat(),
                        )
                        return
                    print(f"[+] No new PRs found for {repo_key}. Already up to date.", file=sys.stderr)
                    state["active_repo"] = repo_key
                    state["active_namespace"] = namespace
                    state["storage_types"][state_key] = storage
                    _set_index_job(
                        job_key,
                        status="ready",
                        documents_indexed=0,
                        truncated_connections=[],
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
                    return

                count = await asyncio.to_thread(
                    index_prs,
                    repo_key,
                    prs,
                    temporary=temporary,
                    namespace=namespace,
                    advance_watermark=not (is_incremental_refresh and not fetch_complete),
                    expected_epoch=expected_epoch,
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
                    _set_index_job(
                        job_key,
                        status="partial",
                        documents_indexed=count,
                        truncated_connections=[],
                        message=(
                            "Refresh reached the page cap. Run ensure_repo_ready "
                            "with refresh=true again to continue."
                        ),
                        finished_at=datetime.now(timezone.utc).isoformat(),
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
                state["active_repo"] = repo_key
                state["active_namespace"] = namespace
                state["storage_types"][state_key] = storage
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
                _set_index_job(
                    job_key,
                    status="ready",
                    documents_indexed=count,
                    truncated_connections=truncated_connections,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                print(f"[+] Background indexing finished for {repo_key}. {count} docs parsed.", file=sys.stderr)
            except IndexInvalidatedError:
                _set_index_job(
                    job_key,
                    status="cancelled",
                    error="Indexing was cancelled because this index was deleted.",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                print(f"[*] Background indexing cancelled for {repo_key} after deletion.", file=sys.stderr)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                _set_index_job(
                    job_key,
                    status="failed",
                    error=error,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                print(f"[!] Background indexing failed for {repo_key}: {error}", file=sys.stderr)

        task = asyncio.create_task(_run_indexing())
        _retain_index_task(job_key, task)

        storage_label = "temporary (in-memory)" if temporary else "permanent (disk)"
        action = "Refresh" if refresh else "Background indexing"
        return (
            f"{action} started for {repo_key} [{storage_label}].\n"
            f"Use the 'get_index_stats' tool to verify its status, document count, and any error.\n"
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
        stats["index_job"] = job
        return json.dumps(stats, indent=2)
