"""Index state computation shared by the indexing and retrieval tools.

This module owns two things:

* the process-local indexing job map (``_INDEX_JOBS``): the user-visible state of
  queued/running/finished background jobs, keyed by repo + namespace + storage;
* :func:`describe_index_state`, which combines that map with the durable
  registry (active generation, backfill state, last job outcome) and the
  coverage object to produce one honest ``index_state`` for a scope.

It deliberately imports nothing heavy at module level (no Chroma, no MCP, no
fetcher): the ``storage`` package is imported lazily inside functions so the
retrieval tools can import this file cheaply and tests can stub storage.

``index_state`` is one of:

* ``no_index`` - no published generation and no job;
* ``indexing`` - a job is queued/running (``serving_generation`` reports the
  generation still serving, if any);
* ``failed`` - no published generation and the last job failed;
* ``ready_selected_scope`` - a generation is published and the last requested
  selection completed (older history may still be outside the selection; see
  ``coverage["older_history_remaining"]``);
* ``ready_partial`` - a generation is published but a requested selection is
  unfinished (capped refresh continuation, or a backfill that stopped early);
* ``stale`` - a generation is published but the last refresh/backfill failed
  (the previous generation keeps serving; a failed refresh stays visible until
  a refresh succeeds, even if a backfill succeeded afterwards) or the index was
  last checked more than 30 days ago.  ``stale_reason`` explains which.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

INDEX_STATES = (
    "no_index",
    "indexing",
    "failed",
    "ready_partial",
    "ready_selected_scope",
    "stale",
)
ERROR_KINDS = (
    "authorization_required",
    "authorization_revoked",
    "rate_limited",
    "repository_not_found",
    "embeddings_unavailable",
    "storage_error",
    "unknown",
)
STALE_AFTER_DAYS = 30
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})

# ── Process-local job map ─────────────────────────────────────────────────────

_INDEX_JOBS: dict[str, dict] = {}
_INDEX_JOBS_LOCK = threading.Lock()


def namespace_scope(namespace: str | None) -> str:
    normalized = namespace.strip() if isinstance(namespace, str) else ""
    return "default:" if not normalized else f"namespace:{normalized}"


def job_key(repo_key: str, namespace: str | None, temporary: bool) -> str:
    storage = "temporary" if temporary else "permanent"
    return f"{repo_key}\0{namespace_scope(namespace)}\0{storage}"


def storage_label(storage) -> str:
    """Accept ``"permanent"``/``"temporary"`` or a ``temporary`` bool."""
    if isinstance(storage, bool):
        return "temporary" if storage else "permanent"
    if storage in (None, ""):
        return "permanent"
    if storage not in {"permanent", "temporary"}:
        raise ValueError("storage must be permanent or temporary")
    return storage


def get_index_job(
    repo_key: str, namespace: str | None, storage=None
) -> dict | None:
    """Return a copy of the newest job for a scope (``storage=None``: either store)."""
    prefix = f"{repo_key}\0{namespace_scope(namespace)}\0"
    suffix = None if storage is None else storage_label(storage)
    with _INDEX_JOBS_LOCK:
        matches = [
            job
            for key, job in _INDEX_JOBS.items()
            if key.startswith(prefix) and (suffix is None or key == prefix + suffix)
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda job: job.get("started_at") or ""))


# ── Error classification ──────────────────────────────────────────────────────

_ENCODER_MODULES = {"sentence_transformers", "torch", "transformers", "huggingface_hub"}
_ENCODER_HINTS = (
    "sentence_transformers",
    "sentencetransformer",
    "all-minilm",
    "huggingface",
    "hf_hub",
)
_REVOKED_HINTS = ("revoked", "expired", "rejected the saved", "connection is invalid", "unauthorized", "bad credentials")


def _mro_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _traceback_files(exc: BaseException) -> list[str]:
    files: list[str] = []
    tb = exc.__traceback__
    while tb is not None:
        files.append((tb.tb_frame.f_code.co_filename or "").replace("\\", "/"))
        tb = tb.tb_next
    return files


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


_STORAGE_FILES = ("/storage/vector_store.py", "/storage/cursor_store.py")
_CREDENTIAL_HINTS = ("credential", "token", "not authenticated", "not connected", "authoriz")


def _raised_in_storage(exc: BaseException) -> bool:
    return any(f.endswith(_STORAGE_FILES) for f in _traceback_files(exc))


def _is_chromadb_error(exc: BaseException) -> bool:
    return any((cls.__module__ or "").startswith("chromadb") for cls in type(exc).__mro__)


def classify_index_error(exc: BaseException) -> str:
    """Map an indexing exception onto one of :data:`ERROR_KINDS`.

    ``storage_error`` is reserved for genuine storage failures: sqlite3 and
    chromadb errors, and OS errors (disk full, permissions) raised inside the
    storage layer.  Passing through a storage module in the traceback is NOT
    enough - a ``KeyError``/``ValueError`` from document building is a data
    problem and stays ``unknown``.
    """
    names = _mro_names(exc)
    text = str(exc).lower()
    from_storage = _raised_in_storage(exc)

    if "GitHubAuthorizationError" in names:
        if any(hint in text for hint in _REVOKED_HINTS):
            return "authorization_revoked"
        return "authorization_required"
    if "RateLimitError" in names:
        return "rate_limited"
    status = _http_status(exc)
    if status == 401:
        return "authorization_revoked"
    if status == 404:
        return "repository_not_found"
    if status == 403:
        return "rate_limited" if "rate limit" in text else "authorization_required"
    if (isinstance(exc, PermissionError) and not from_storage) or (
        "rejected the saved credential" in text
    ):
        return "authorization_revoked"
    if "rate limit" in text:
        return "rate_limited"
    if "not found or inaccessible" in text or "could not resolve to a repository" in text:
        return "repository_not_found"

    files = _traceback_files(exc)
    if any(f.endswith("/storage/encoder.py") for f in files):
        return "embeddings_unavailable"
    if getattr(exc, "name", None) in _ENCODER_MODULES and isinstance(exc, ImportError):
        return "embeddings_unavailable"
    if any(hint in text for hint in _ENCODER_HINTS):
        return "embeddings_unavailable"

    if isinstance(exc, sqlite3.Error) or _is_chromadb_error(exc):
        return "storage_error"
    if isinstance(exc, OSError):  # EnvironmentError is an alias of OSError
        if from_storage:
            return "storage_error"
        if any(hint in text for hint in _CREDENTIAL_HINTS):
            return "authorization_required"
    return "unknown"


def action_for(error_kind: str | None, exc: BaseException | None = None) -> str:
    """Return the actionable next step for a job failure."""
    if error_kind == "authorization_required":
        return (
            "Run begin_github_authorization then complete_github_authorization "
            "to connect GitHub, then retry ensure_repo_ready."
        )
    if error_kind == "authorization_revoked":
        return (
            "The saved GitHub connection was rejected or has expired. Run "
            "disconnect_github, then begin_github_authorization and "
            "complete_github_authorization, then retry ensure_repo_ready."
        )
    if error_kind == "rate_limited":
        reset_at = getattr(exc, "reset_at", None)
        when = ""
        if isinstance(reset_at, (int, float)):
            when = f" (resets at {datetime.fromtimestamp(reset_at, timezone.utc).isoformat()})"
        return f"GitHub rate limit reached{when}. Wait for it to reset, then retry ensure_repo_ready."
    if error_kind == "repository_not_found":
        return (
            "GitHub could not find this repository with the current connection. Check the "
            "owner/name and that the GitHub App can access it, then retry ensure_repo_ready."
        )
    if error_kind == "embeddings_unavailable":
        return (
            "The embedding model could not be loaded. Check the network or the local "
            "model cache and that sentence-transformers is installed, then retry "
            "ensure_repo_ready. The existing index keeps serving."
        )
    if error_kind == "storage_error":
        return (
            "The index storage failed. Check free disk space and permissions of the index "
            "directory, then retry ensure_repo_ready. The previous generation keeps serving."
        )
    return "Retry ensure_repo_ready; if it keeps failing, check the server log for the error."


def error_details(exc: BaseException) -> dict:
    kind = classify_index_error(exc)
    return {"error_kind": kind, "action": action_for(kind, exc)}


# ── State computation ─────────────────────────────────────────────────────────

_PUBLIC_JOB_KEYS = (
    "status", "kind", "repo", "namespace", "storage", "refresh", "backfill",
    "started_at", "finished_at", "documents_indexed", "error", "error_kind",
    "action", "message", "history_complete", "older_history_remaining",
    "pages",
)


def _public_job(job: dict | None, registry: dict) -> dict | None:
    if job:
        public = {key: job.get(key) for key in _PUBLIC_JOB_KEYS if key in job}
        public["source"] = "process"
        return public
    if registry.get("last_job_status"):
        return {
            "status": registry.get("last_job_status"),
            "kind": None,
            "error_kind": registry.get("last_job_error_kind"),
            "finished_at": registry.get("last_job_finished_at"),
            "source": "registry",
        }
    return None


def _days_since(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).days


def describe_index_state(repo: str, namespace: str | None = None, storage="permanent") -> dict:
    """Return the honest state of one index scope.

    Result keys: ``index_state``, ``stale_reason``, ``coverage``, ``generation``
    (active generation, 0 = none/legacy), ``serving_generation`` (``None`` when
    nothing is published), ``last_job`` (in-process job or registry summary) and
    ``action`` (what the user should do next, or ``None``).
    """
    import storage as storage_pkg

    label = storage_label(storage)
    registry = storage_pkg.get_index_registry(repo, namespace, label)
    coverage = storage_pkg.compute_coverage(repo, namespace, label)
    generation = int(coverage.get("generation") or 0)
    has_index = generation > 0 or int(coverage.get("evidence_units") or 0) > 0

    job = get_index_job(repo, namespace, label)
    if job and job.get("status") == "cancelled":
        job = None  # a deleted index's job says nothing about the current scope
    last_job = _public_job(job, registry)
    status = (last_job or {}).get("status")
    error_kind = (last_job or {}).get("error_kind")

    stale_reason: str | None = None
    action: str | None = None
    if status in ACTIVE_JOB_STATUSES:
        state = "indexing"
    elif not has_index:
        if status == "failed":
            state = "failed"
            action = (last_job or {}).get("action") or action_for(error_kind)
        else:
            state = "no_index"
            action = (
                "Run ensure_repo_ready (choose permanent or temporary storage) "
                "to index this repository."
            )
    else:
        days_old = _days_since(coverage.get("checked_at"))
        # Refresh health is durable and independent of the latest job: a later
        # successful backfill must not hide an earlier failed refresh.  It is
        # cleared only when a refresh (or the initial import) succeeds.
        refresh_failed = registry.get("last_refresh_status") == "failed"
        job_failed = status == "failed"
        if job_failed or refresh_failed:
            state = "stale"
            if job_failed:
                kind = (last_job or {}).get("kind")
                failed_kind = error_kind
                what = f"The last {kind or 'refresh/backfill'} job"
                action = (last_job or {}).get("action") or action_for(error_kind)
            else:
                failed_kind = registry.get("last_refresh_error_kind")
                what = "The last refresh"
                action = action_for(failed_kind)
            when = registry.get("last_refresh_finished_at") if not job_failed else None
            stale_reason = (
                f"{what} failed"
                f"{f' ({failed_kind})' if failed_kind else ''}"
                f"{f' at {when}' if when else ''}; generation {generation} "
                "from the previous successful job is still being served."
            )
        elif days_old is not None and days_old > STALE_AFTER_DAYS:
            state = "stale"
            stale_reason = (
                f"The index was last checked against GitHub {days_old} days ago "
                f"(more than {STALE_AFTER_DAYS})."
            )
            action = "Run ensure_repo_ready with refresh=true to update this index."
        elif status == "partial" or registry.get("refresh_cursor"):
            state = "ready_partial"
            if registry.get("refresh_cursor"):
                action = "Run ensure_repo_ready with refresh=true to finish the capped refresh."
            else:
                action = (
                    "Run ensure_repo_ready with backfill=true to continue importing "
                    "older history."
                )
        else:
            state = "ready_selected_scope"

    return {
        "index_state": state,
        "stale_reason": stale_reason,
        "coverage": coverage,
        "generation": generation,
        "serving_generation": generation if has_index else None,
        "last_job": last_job,
        "action": action,
    }


def format_index_status(description: dict) -> str:
    """Human-readable lines for an index description (used by tool responses)."""
    generation = description.get("serving_generation")
    head = f"Index state: {description['index_state']}"
    if generation is not None:
        head += f" (generation {generation})"
    lines = [head]
    coverage = description.get("coverage") or {}
    if coverage.get("summary"):
        lines.append(f"Coverage: {coverage['summary']}")
    if description.get("stale_reason"):
        lines.append(f"Stale: {description['stale_reason']}")
    if description.get("action"):
        lines.append(f"Action: {description['action']}")
    return "\n".join(lines)
