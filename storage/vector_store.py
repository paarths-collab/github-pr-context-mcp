# ChromaDB client management, generation-based indexing, querying, coverage.
# No ML model loading, no PR transformation, no GitHub calls here.
#
# Generation model
# ----------------
# Each write stages a NEW Chroma collection ``<gbase>-g<N>``: the previous
# generation's documents/embeddings/metadatas are copied (never re-encoded),
# the batch's upserts and reconcile-deletes are applied, the result is
# validated, and only then published by flipping ``active_generation`` in the
# registry (cursor_store) inside one SQLite transaction.  Older collections are
# dropped except the immediately previous one.  A failure before publish
# deletes the staged collection and leaves the previous generation serving.
# A legacy unsuffixed collection ``<base>`` (v2/v3 indexes, migrated indexes)
# is a readable "generation 0" until the first new write supersedes it.

import chromadb
import datetime
import hashlib
import json
import math
import os
import re
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from dotenv import load_dotenv
from storage.encoder import encode, encode_batch
from storage.document_builder import build_documents

load_dotenv()

_DEFAULT_CHROMA_DIR = os.path.join(os.path.expanduser("~"), ".github-pr-mcp", "chroma_db")
PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", _DEFAULT_CHROMA_DIR)

# Persistent = survives restarts, stored on disk
_persistent_client = chromadb.PersistentClient(path=PERSIST_DIR)

# Ephemeral = in-memory only, wiped when the MCP server process stops
_ephemeral_client = chromadb.EphemeralClient()

# Re-entrant so read helpers can compose (e.g. coverage inside a stats call).
_chroma_lock = threading.RLock()
_INDEX_EPOCHS: dict[str, int] = {}

_COPY_BATCH = 500
_GENERATION_RE_TEMPLATE = r"^{base}-g(\d+)$"


class IndexInvalidatedError(RuntimeError):
    """Raised when a deleted index job attempts a stale write."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize_namespace(namespace: str | None) -> str | None:
    if namespace is None:
        return None
    ns = namespace.strip()
    return ns or None


def _index_scope_key(repo_key: str, temporary: bool, namespace: str | None) -> str:
    ns = _normalize_namespace(namespace)
    scope = "default:" if ns is None else f"namespace:{ns}"
    storage = "temporary" if temporary else "permanent"
    return f"{repo_key}\0{scope}\0{storage}"


def get_index_epoch(
    repo_key: str,
    temporary: bool = False,
    namespace: str | None = None,
) -> int:
    """Capture the current in-process write epoch for an index scope."""
    with _chroma_lock:
        return _INDEX_EPOCHS.get(_index_scope_key(repo_key, temporary, namespace), 0)


def _assert_current_epoch(
    repo_key: str,
    temporary: bool,
    namespace: str | None,
    expected_epoch: int | None,
) -> None:
    if expected_epoch is None:
        return
    current_epoch = _INDEX_EPOCHS.get(_index_scope_key(repo_key, temporary, namespace), 0)
    if current_epoch != expected_epoch:
        raise IndexInvalidatedError(
            f"Index job for {repo_key} was invalidated by a delete request."
        )


def _safe_name(repo_key: str) -> str:
    safe = re.sub(r"[^a-z0-9._-]+", "-", repo_key.lower()).strip(".-")
    if not safe:
        return "repo"
    return safe if safe[0].isalnum() else f"repo-{safe}"


def _scope_digest(repo_key: str, namespace: str | None) -> str:
    ns = _normalize_namespace(namespace)
    scope = "default:" if ns is None else f"namespace:{ns}"
    return hashlib.sha256(f"{repo_key}\0{scope}".encode("utf-8")).hexdigest()[:12]


def _collection_name(repo_key: str, namespace: str | None = None) -> str:
    """Return the stable LEGACY (generation 0) Chroma name for a scope.

    A separate collection prevents one namespace from seeing, overwriting, or
    deleting another namespace's documents for the same GitHub repository.
    The digest avoids leaking the raw namespace in a collection name and keeps
    the result inside Chroma's 63-character collection-name limit.
    """
    return f"{_safe_name(repo_key)[:48]}--{_scope_digest(repo_key, namespace)}"


def _generation_base(repo_key: str, namespace: str | None = None) -> str:
    """Base for ``<base>-g<N>`` names (shortened so ``-g<N>`` fits in 63 chars)."""
    return f"{_safe_name(repo_key)[:40].rstrip('.-_') or 'repo'}--{_scope_digest(repo_key, namespace)}"


def _generation_name(repo_key: str, generation: int, namespace: str | None = None) -> str:
    return f"{_generation_base(repo_key, namespace)}-g{int(generation)}"


def _collection_metadata(repo_key: str, namespace: str | None = None) -> dict:
    metadata = {
        "hnsw:space": "cosine",
        "repo": repo_key,
    }
    ns = _normalize_namespace(namespace)
    if ns is not None:
        metadata["namespace"] = ns
    return metadata


def _collection_repo(col) -> str:
    meta = col.metadata or {}
    if "repo" in meta:
        return meta["repo"]
    return col.name.replace("--", "/")


def _collection_namespace(col) -> str | None:
    meta = col.metadata or {}
    ns = meta.get("namespace")
    return _normalize_namespace(ns) if isinstance(ns, str) else None


def _client(temporary: bool):
    return _ephemeral_client if temporary else _persistent_client


def _storage_label(storage) -> str:
    """Accept "permanent"/"temporary" (or a ``temporary`` bool) for new APIs."""
    if isinstance(storage, bool):
        return "temporary" if storage else "permanent"
    if storage not in {"permanent", "temporary"}:
        raise ValueError("storage must be permanent or temporary")
    return storage


def _registry():
    from storage.cursor_store import cursor_store

    return cursor_store


def _collection_names(client) -> list[str]:
    names = []
    for item in client.list_collections():
        names.append(item if isinstance(item, str) else getattr(item, "name", ""))
    return names


def _existing_generations(client, repo_key: str, namespace: str | None) -> dict[int, str]:
    pattern = re.compile(
        _GENERATION_RE_TEMPLATE.format(base=re.escape(_generation_base(repo_key, namespace)))
    )
    found: dict[int, str] = {}
    for name in _collection_names(client):
        match = pattern.match(name)
        if match:
            found[int(match.group(1))] = name
    return found


@dataclass
class _Active:
    """The collection currently serving reads for a scope."""

    generation: int  # 0 for the legacy unsuffixed collection
    collection: object
    legacy: bool
    name: str


def _resolve_active(
    repo_key: str, temporary: bool = False, namespace: str | None = None
) -> "_Active | None":
    """Locate the collection that serves reads (never creates anything)."""
    client = _client(temporary)
    registry = _registry().get_registry(
        repo_key, namespace=namespace, storage="temporary" if temporary else "permanent"
    )
    number = int(registry.get("active_generation") or 0)
    if number > 0:
        name = _generation_name(repo_key, number, namespace)
        try:
            return _Active(number, client.get_collection(name), False, name)
        except Exception:
            pass  # registry points at missing data; fall back to legacy below
    legacy_name = _collection_name(repo_key, namespace)
    try:
        return _Active(0, client.get_collection(legacy_name), True, legacy_name)
    except Exception:
        return None


def _get_collection(repo_key: str, temporary: bool = False, namespace: str | None = None):
    """Return the active collection, creating an empty legacy one when none exists."""
    active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
    if active is not None:
        return active.collection
    return _client(temporary).get_or_create_collection(
        name=_collection_name(repo_key, namespace),
        metadata=_collection_metadata(repo_key, namespace=namespace),
    )


def _as_list(vector) -> list[float]:
    if hasattr(vector, "tolist"):
        return vector.tolist()
    return list(vector)


def get_active_generation(
    repo_key: str, namespace: str | None = None, storage="permanent"
) -> int:
    """Return the serving generation number (0 = none or legacy unsuffixed)."""
    temporary = _storage_label(storage) == "temporary"
    with _chroma_lock:
        active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
    return active.generation if active else 0


# ── Status checks ─────────────────────────────────────────────────────────────

def _is_indexed(repo_key: str, temporary: bool, namespace: str | None) -> bool:
    try:
        with _chroma_lock:
            active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
            return active is not None and active.collection.count() > 0
    except Exception:
        return False


def repo_is_indexed_permanently(repo_key: str, namespace: str | None = None) -> bool:
    return _is_indexed(repo_key, False, namespace)


def repo_is_indexed_temporarily(repo_key: str, namespace: str | None = None) -> bool:
    return _is_indexed(repo_key, True, namespace)


# ── Listing ───────────────────────────────────────────────────────────────────

def list_all_repos(namespace: str | None = None) -> list[dict]:
    ns_filter = _normalize_namespace(namespace)

    def _rows(client, storage_label: str) -> list[dict]:
        temporary = storage_label == "temporary"
        scopes: dict[tuple[str, str | None], None] = {}
        for item in client.list_collections():
            col = client.get_collection(item) if isinstance(item, str) else item
            repo = _collection_repo(col)
            repo_ns = _collection_namespace(col)
            if ns_filter is not None and repo_ns != ns_filter:
                continue
            scopes[(repo, repo_ns)] = None
        items = []
        for repo, repo_ns in scopes:
            active = _resolve_active(repo, temporary=temporary, namespace=repo_ns)
            if active is None:
                continue
            items.append({
                "repo": repo,
                "namespace": repo_ns,
                "total_documents": active.collection.count(),
                "storage": storage_label,
                "generation": active.generation,
            })
        return items

    with _chroma_lock:
        permanent = _rows(_persistent_client, "permanent")
        temporary = _rows(_ephemeral_client, "temporary")
    return permanent + temporary


# ── Indexing ──────────────────────────────────────────────────────────────────

_THREAD_TYPES = {"review_thread", "review_comment"}  # review_comment = legacy per-comment units
_THREAD_TRUNCATIONS = {"reviewThreads", "reviewThreads.comments"}


def _truncated_set(pr: dict) -> set[str]:
    truncated = pr.get("truncated_connections")
    return {str(v) for v in truncated} if isinstance(truncated, list) else set()


def _unit_source_key(unit_id: str, metadata: dict | None) -> str:
    """Return the source key of a unit (its id minus the trailing content digest)."""
    key = (metadata or {}).get("source_key")
    if isinstance(key, str) and key:
        return key
    return unit_id.rsplit(":", 1)[0] if ":" in unit_id else unit_id


def _stale_document_ids(collection, prs: list[dict], ids: list[str], metadatas: list[dict]) -> list[str]:
    """Find previous units of a re-indexed PR that the new build supersedes.

    Reconciliation rule (per PR present in the new batch): REPLACE the PR's
    previous units wholesale - every previous unit whose id is not in the new
    build is deleted (descriptions, review threads, review summaries, commits,
    legacy units), so an edited thread never lingers as a second version.
    The one exception protects against partial GitHub responses: for a group
    whose connection is flagged truncated on the new PR record (``reviewThreads``
    / ``reviewThreads.comments`` for threads, ``reviews``, ``commits``), old
    units whose source was NOT re-returned are kept (GitHub may simply not have
    sent them), while any old version of a source that WAS re-returned is
    still deleted because the new fetch supersedes it.
    """
    current_ids: set[str] = set(ids)
    new_keys: dict[tuple[int, str], set[str]] = {}
    for unit_id, metadata in zip(ids, metadatas):
        pr_number = metadata.get("pr_number")
        doc_type = metadata.get("type")
        if isinstance(pr_number, int) and isinstance(doc_type, str):
            new_keys.setdefault((pr_number, doc_type), set()).add(_unit_source_key(unit_id, metadata))

    stale: list[str] = []
    for pr in prs:
        pr_number = pr.get("number")
        if not isinstance(pr_number, int):
            continue
        truncated = _truncated_set(pr)
        protected = set()
        if truncated & _THREAD_TRUNCATIONS:
            protected |= _THREAD_TYPES
        if "commits" in truncated:
            protected.add("commit_message")
        if "reviews" in truncated:
            protected.add("review_summary")
        existing = collection.get(where={"pr_number": pr_number}, include=["metadatas"])
        for existing_id, metadata in zip(
            existing.get("ids") or [], existing.get("metadatas") or []
        ):
            if existing_id in current_ids:
                continue
            doc_type = metadata.get("type") if isinstance(metadata, dict) else None
            if not isinstance(doc_type, str):
                continue
            if doc_type in protected:
                same_group = new_keys.get((pr_number, doc_type), set())
                if doc_type in _THREAD_TYPES:
                    same_group = same_group | new_keys.get(
                        (pr_number, "review_thread" if doc_type == "review_comment" else "review_comment"),
                        set(),
                    )
                if _unit_source_key(existing_id, metadata) not in same_group:
                    continue  # partial response: keep what was not re-returned
            stale.append(existing_id)
    return stale


def _pr_ledger_row(pr: dict) -> dict | None:
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    nested = pr.get("nested_incomplete_threads")
    nested = nested if isinstance(nested, int) and not isinstance(nested, bool) else 0
    if "reviewThreads" in _truncated_set(pr):
        nested = max(nested, 1)  # unfetched threads exist: the PR is at least partially covered
    return {
        "pr_number": number,
        "created_at": pr.get("created_at") if isinstance(pr.get("created_at"), str) else "",
        "updated_at": pr.get("updated_at") if isinstance(pr.get("updated_at"), str) else "",
        "state": pr.get("state") if isinstance(pr.get("state"), str) else "",
        "nested_incomplete_threads": nested,
    }


def _meta_truncated_connections(meta: dict) -> list:
    raw = meta.get("truncated_connections")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def _ledger_rows_from_metadata(metadatas) -> dict[int, dict]:
    """Derive per-PR ledger rows from document metadata (legacy / ledger-less indexes).

    ``nested_incomplete_threads`` counts DISTINCT incomplete threads (a split
    thread's parts count once) and is at least 1 for a PR whose reviewThreads
    connection was truncated.  ``created_at`` stays "" when no unit carries
    ``pr_created_at`` so callers never invent a date from a partial set.
    """
    rows: dict[int, dict] = {}
    incomplete: dict[int, set] = {}
    anonymous: dict[int, int] = {}
    threads_truncated: set[int] = set()
    for meta in metadatas:
        if not isinstance(meta, dict):
            continue
        number = meta.get("pr_number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        row = rows.setdefault(
            number,
            {
                "pr_number": number,
                "created_at": "",
                "updated_at": "",
                "state": "",
                "nested_incomplete_threads": 0,
            },
        )
        created = meta.get("pr_created_at")
        if isinstance(created, str) and created and not row["created_at"]:
            row["created_at"] = created
        updated = meta.get("pr_updated_at") or meta.get("updated_at")
        if isinstance(updated, str) and updated and updated > row["updated_at"]:
            row["updated_at"] = updated
        state = meta.get("pr_state")
        if isinstance(state, str) and state and not row["state"]:
            row["state"] = state
        if meta.get("thread_incomplete") is True:
            thread_key = meta.get("thread_evidence_id") or meta.get("evidence_id")
            if isinstance(thread_key, str) and thread_key:
                incomplete.setdefault(number, set()).add(thread_key)
            else:
                anonymous[number] = anonymous.get(number, 0) + 1
        if "reviewThreads" in _meta_truncated_connections(meta):
            threads_truncated.add(number)
    for number, row in rows.items():
        count = len(incomplete.get(number, ())) + anonymous.get(number, 0)
        if number in threads_truncated:
            count = max(count, 1)
        row["nested_incomplete_threads"] = count
    return rows


def _iter_records(collection, include: list[str], where: dict | None = None):
    """Yield batches of a collection via Chroma's public paging API."""
    offset = 0
    while True:
        kwargs = {"include": include, "limit": _COPY_BATCH, "offset": offset}
        if where:
            kwargs["where"] = where
        batch = collection.get(**kwargs)
        ids = batch.get("ids") or []
        if not ids:
            return
        yield batch
        offset += len(ids)
        if len(ids) < _COPY_BATCH:
            return


def _copy_generation(source, staged) -> tuple[set[str], dict[int, dict]]:
    """Copy documents + embeddings + metadatas verbatim (no re-encoding)."""
    copied: set[str] = set()
    all_metadatas: list[dict] = []
    for batch in _iter_records(source, ["documents", "metadatas", "embeddings"]):
        ids = list(batch["ids"])
        documents = list(batch.get("documents") or [])
        metadatas = [dict(m) if isinstance(m, dict) else {} for m in (batch.get("metadatas") or [])]
        embeddings = [_as_list(e) for e in (batch.get("embeddings") if batch.get("embeddings") is not None else [])]
        if not (len(ids) == len(documents) == len(metadatas) == len(embeddings)):
            raise RuntimeError("previous generation returned misaligned records")
        staged.upsert(documents=documents, embeddings=embeddings, metadatas=metadatas, ids=ids)
        copied.update(ids)
        all_metadatas.extend(metadatas)
    return copied, _ledger_rows_from_metadata(all_metadatas)


def _safe_delete_collection(client, name: str) -> bool:
    try:
        client.delete_collection(name)
        return True
    except Exception:
        return False


def _prune_generations(
    client, repo_key: str, namespace: str | None, keep_generations: set[int]
) -> None:
    """Drop every generation except ``keep_generations`` (0 = legacy collection)."""
    for number, name in _existing_generations(client, repo_key, namespace).items():
        if number not in keep_generations:
            _safe_delete_collection(client, name)
    if 0 not in keep_generations:
        _safe_delete_collection(client, _collection_name(repo_key, namespace))


def index_prs(
    repo_key: str,
    prs: list[dict],
    temporary: bool = False,
    namespace: str | None = None,
    advance_watermark: bool = True,
    expected_epoch: int | None = None,
    is_refresh: bool | None = None,
) -> int:
    """Stage, validate and atomically publish a new generation for a GitHub batch.

    Returns the number of evidence units built from ``prs``.  ``is_refresh``
    marks the generation as one produced by an incremental refresh (used for the
    coverage ``selection``).  It is never inferred: None leaves the registry's
    existing refresh flag untouched (so chunked initial imports and legacy
    upgrades are not labelled "+refresh").
    """
    storage = "temporary" if temporary else "permanent"
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        registry = _registry()
        client = _client(temporary)

        docs, metadatas, ids = build_documents(prs)
        ns = _normalize_namespace(namespace)
        for meta in metadatas:
            if ns:
                meta["namespace"] = ns
        # Encode before touching storage: an embedding failure creates nothing.
        embeddings = encode_batch(docs) if docs else []

        previous = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
        reg_state = registry.get_registry(repo_key, namespace=namespace, storage=storage)
        registry_active = int(reg_state.get("active_generation") or 0)
        existing = _existing_generations(client, repo_key, namespace)
        new_generation = max([registry_active, *existing.keys(), 0]) + 1
        staged_name = _generation_name(repo_key, new_generation, namespace)
        published_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        staged = None
        try:
            staged_meta = _collection_metadata(repo_key, namespace=namespace)
            staged_meta.update({
                "generation": new_generation,
                "last_indexed_at": published_at,
            })
            staged = client.get_or_create_collection(name=staged_name, metadata=staged_meta)

            copied_ids: set[str] = set()
            derived_rows: dict[int, dict] = {}
            ledger_base = registry_active
            if previous is not None:
                copied_ids, derived_rows = _copy_generation(previous.collection, staged)
                if previous.generation != registry_active:
                    ledger_base = 0  # registry pointed at vanished data
                needs_derived = not registry.get_ledger(
                    repo_key, namespace=namespace, storage=storage, generation=ledger_base
                )
                if not needs_derived:
                    derived_rows = {}
            elif registry_active:
                ledger_base = 0

            stale_ids = _stale_document_ids(staged, prs, ids, metadatas)
            if docs:
                staged.upsert(documents=docs, embeddings=embeddings, metadatas=metadatas, ids=ids)
            if stale_ids:
                staged.delete(ids=stale_ids)

            expected_ids = (copied_ids | set(ids)) - set(stale_ids)
            actual_ids = set((staged.get(include=[]) or {}).get("ids") or [])
            if actual_ids != expected_ids or staged.count() != len(expected_ids):
                raise RuntimeError(
                    f"Generation {new_generation} failed validation: expected "
                    f"{len(expected_ids)} documents, found {len(actual_ids)}."
                )

            ledger_upserts = list(derived_rows.values())
            for pr in prs:
                row = _pr_ledger_row(pr)
                if row is not None:
                    ledger_upserts.append(row)  # later rows override derived ones

            # Explicit ``is_refresh`` is the source of truth.  When it is None the
            # generation is NOT labelled a refresh by inference (a chunked initial
            # import or a legacy upgrade is indistinguishable from a refresh by
            # shape); the registry's existing flag - set by complete_refresh() or
            # an earlier is_refresh=True publish - is simply left unchanged.
            refresh_flag = bool(is_refresh)
            # Only a write that synced with GitHub's newest state may reset the
            # stale clock: a refresh, the initial import of a scope, or a legacy
            # caller that did not say (is_refresh None + advance_watermark).  An
            # older-history backfill (is_refresh False on an existing generation)
            # must not, or it would hide that the index is out of date.
            sets_checked = bool(
                is_refresh
                or not registry_active
                or (is_refresh is None and advance_watermark)
            )

            # A delete during staging bumps the epoch; never publish over it.
            _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
            registry.publish_generation(
                repo_key,
                namespace=namespace,
                storage=storage,
                generation=new_generation,
                previous_generation=registry_active,
                ledger_base_generation=ledger_base,
                ledger_upserts=ledger_upserts,
                published_at=published_at,
                refresh_applied=True if refresh_flag else None,
                mark_checked=sets_checked,
            )
        except BaseException:
            _safe_delete_collection(client, staged_name)
            raise

        # Published. Keep only the new generation and the previous one.
        keep = {new_generation}
        if previous is not None:
            keep.add(previous.generation)
        try:
            _prune_generations(client, repo_key, namespace, keep)
        except Exception as exc:  # pruning is best-effort; serving is unaffected
            print(f"Generation pruning skipped for {repo_key}: {exc}", file=sys.stderr)

        # Advance only after publish succeeds. A failed embedding, staging or
        # publish therefore remains eligible for the next refresh.
        if prs:
            max_pr = max((pr.get("number", 0) for pr in prs), default=0)
            if max_pr > 0:
                updated_at_values = [
                    value
                    for pr in prs
                    if isinstance((value := pr.get("updated_at")), str) and value
                ]
                registry.set_cursor(
                    repo_key,
                    max_pr,
                    namespace=namespace,
                    storage=storage,
                    last_updated_at=(
                        max(updated_at_values, default=None)
                        if advance_watermark
                        else None
                    ),
                )

    label = "temporary (in-memory)" if temporary else "permanent (disk)"
    print(
        f"Indexed {len(docs)} documents for {repo_key} [{label}] generation {new_generation}",
        file=sys.stderr,
    )
    return len(docs)


def mark_repo_checked(
    repo_key: str,
    temporary: bool = False,
    namespace: str | None = None,
    expected_epoch: int | None = None,
) -> None:
    """Record a successful GitHub sync even if it produced no new documents."""
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        _registry().mark_checked(
            repo_key,
            namespace=namespace,
            storage="temporary" if temporary else "permanent",
        )


def checkpoint_refresh(
    repo_key: str,
    *,
    temporary: bool = False,
    namespace: str | None = None,
    next_cursor: str,
    high_watermark: str | None,
    expected_epoch: int | None = None,
) -> None:
    """Persist a partial refresh only when its index scope is still current."""
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        _registry().checkpoint_refresh(
            repo_key,
            namespace=namespace,
            storage="temporary" if temporary else "permanent",
            next_cursor=next_cursor,
            high_watermark=high_watermark,
        )


def complete_refresh(
    repo_key: str,
    last_pr_number: int,
    *,
    temporary: bool = False,
    namespace: str | None = None,
    high_watermark: str | None,
    expected_epoch: int | None = None,
) -> None:
    """Commit a completed refresh only when its index scope is still current."""
    with _chroma_lock:
        _assert_current_epoch(repo_key, temporary, namespace, expected_epoch)
        _registry().complete_refresh(
            repo_key,
            last_pr_number,
            namespace=namespace,
            storage="temporary" if temporary else "permanent",
            high_watermark=high_watermark,
        )


# ── Backfill state + last job outcome (epoch protected) ───────────────────────

_UNSET = object()


def set_backfill_state(
    repo_key: str,
    namespace: str | None = None,
    storage="permanent",
    *,
    backfill_cursor=_UNSET,
    history_complete=_UNSET,
    pages_done=_UNSET,
    add_pages: int = 0,
    expected_epoch: int | None = None,
) -> None:
    """Persist older-history backfill progress (cursor, completeness, pages).

    Only the provided fields change.  ``history_complete`` is tri-state
    (True / False / None = unknown).  Never moves the refresh watermark.
    """
    label = _storage_label(storage)
    with _chroma_lock:
        _assert_current_epoch(repo_key, label == "temporary", namespace, expected_epoch)
        kwargs = {}
        if backfill_cursor is not _UNSET:
            kwargs["backfill_cursor"] = backfill_cursor
        if history_complete is not _UNSET:
            kwargs["history_complete"] = history_complete
        if pages_done is not _UNSET:
            kwargs["pages_done"] = pages_done
        _registry().set_backfill_state(
            repo_key, namespace=namespace, storage=label, add_pages=add_pages, **kwargs
        )


def record_job_outcome(
    repo_key: str,
    namespace: str | None = None,
    storage="permanent",
    *,
    status: str,
    error_kind: str | None = None,
    finished_at: str | None = None,
    expected_epoch: int | None = None,
    kind: str | None = None,
) -> None:
    """Record the last indexing job outcome (status, error_kind, finish time).

    ``kind`` ("initial" / "refresh" / "backfill") routes refresh and initial
    outcomes into the separate refresh-health fields as well.
    """
    label = _storage_label(storage)
    with _chroma_lock:
        _assert_current_epoch(repo_key, label == "temporary", namespace, expected_epoch)
        _registry().record_job_outcome(
            repo_key,
            namespace=namespace,
            storage=label,
            status=status,
            error_kind=error_kind,
            finished_at=finished_at,
            kind=kind,
        )


def get_index_registry(
    repo_key: str, namespace: str | None = None, storage="permanent"
) -> dict:
    """Return the registry row (generation, watermarks, backfill, last job)."""
    return _registry().get_registry(repo_key, namespace=namespace, storage=_storage_label(storage))


# ── Coverage ──────────────────────────────────────────────────────────────────

_BOUNDED_PHRASE = "bounded history window, not full repository history"


def _day(value: str | None) -> str | None:
    return value[:10] if isinstance(value, str) and value else None


def _truncated_pull_requests(meta: dict) -> bool:
    raw = meta.get("truncated_connections")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except ValueError:
            return "pullRequests" in meta["truncated_connections"]
    return isinstance(raw, list) and "pullRequests" in raw


def compute_coverage(
    repo_key: str, namespace: str | None = None, storage="permanent"
) -> dict:
    """Describe exactly which part of the repository history is indexed.

    Computed from the PR ledger + registry of the ACTIVE generation, independent
    of whether the last job succeeded.  Legacy indexes (no ledger) are derived
    from document metadata and report ``history_complete`` None.
    """
    label = _storage_label(storage)
    temporary = label == "temporary"
    with _chroma_lock:
        registry = _registry()
        reg = registry.get_registry(repo_key, namespace=namespace, storage=label)
        active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
        units = 0
        derived = False
        pull_requests_truncated = False
        if active is not None:
            units = active.collection.count()
            summary_row = registry.ledger_summary(
                repo_key, namespace=namespace, storage=label, generation=active.generation
            )
        else:
            summary_row = registry.ledger_summary(
                repo_key, namespace=namespace, storage=label, generation=-1
            )
        if active is not None and summary_row["count"] > 0 and any(
            not r.get("created_at")
            for r in registry.get_ledger(
                repo_key, namespace=namespace, storage=label, generation=active.generation
            )
        ):
            # Ledger rows derived from legacy documents may lack a creation date;
            # never claim an oldest/newest created date from the partial set.
            summary_row = {
                **summary_row, "oldest_pr_created_at": None, "newest_pr_created_at": None
            }
        if active is not None and summary_row["count"] == 0 and units > 0:
            derived = True
            rows: dict[int, dict] = {}
            metas: list[dict] = []
            for batch in _iter_records(active.collection, ["metadatas"]):
                for meta in batch.get("metadatas") or []:
                    if isinstance(meta, dict):
                        metas.append(meta)
                        if _truncated_pull_requests(meta):
                            pull_requests_truncated = True
            rows = _ledger_rows_from_metadata(metas)
            created = sorted(r["created_at"] for r in rows.values() if r["created_at"])
            updated = sorted(r["updated_at"] for r in rows.values() if r["updated_at"])
            numbers = list(rows)
            if any(not r["created_at"] for r in rows.values()):
                created = []  # a partial set must not claim an oldest/newest created date
            summary_row = {
                "count": len(rows),
                "pr_number_min": min(numbers) if numbers else None,
                "pr_number_max": max(numbers) if numbers else None,
                "oldest_pr_created_at": created[0] if created else None,
                "newest_pr_created_at": created[-1] if created else None,
                "newest_pr_updated_at": updated[-1] if updated else None,
                "states": sorted({r["state"] for r in rows.values() if r["state"]}),
                "nested_incomplete_threads": sum(
                    r["nested_incomplete_threads"] for r in rows.values()
                ),
            }

    history_complete = reg.get("history_complete")
    # Unknown (None) completeness counts as "older history may remain".
    older_remaining = (
        history_complete is not True
        or bool(reg.get("backfill_cursor"))
        or (derived and pull_requests_truncated and history_complete is not True)
    )
    if history_complete is True:
        older_remaining = False
        selection = "full-closed-history"
    elif reg.get("refresh_applied"):
        selection = "closed-created-window+refresh"
    else:
        selection = "closed-created-window"

    count = summary_row["count"]
    nested = summary_row["nested_incomplete_threads"]
    if count == 0:
        summary = "No closed pull requests are indexed for this repository scope yet"
        if units:
            summary += f" ({units} evidence units lack pull request provenance)"
        summary += "."
    else:
        newest, oldest = _day(summary_row["newest_pr_created_at"]), _day(summary_row["oldest_pr_created_at"])
        window = f", created {oldest} to {newest}" if oldest and newest else ""
        summary = (
            f"Indexed {count} closed pull request{'s' if count != 1 else ''} "
            f"(#{summary_row['pr_number_min']} to #{summary_row['pr_number_max']}{window}) "
            f"as {units} evidence units"
        )
        if nested:
            summary += (
                f", with {nested} review thread{'s' if nested != 1 else ''} "
                "only partially fetched"
            )
        if history_complete is True:
            summary += ", covering the full closed pull request history"
        else:
            summary += f", a {_BOUNDED_PHRASE}"
            if history_complete is None:
                summary += "; history completeness is unknown, so older history may remain"
        if derived:
            summary += " (derived from document metadata; this index has no coverage ledger)"
        summary += "."

    return {
        "selection": selection,
        "pull_requests_indexed": count,
        "pr_number_min": summary_row["pr_number_min"],
        "pr_number_max": summary_row["pr_number_max"],
        "oldest_pr_created_at": summary_row["oldest_pr_created_at"],
        "newest_pr_created_at": summary_row["newest_pr_created_at"],
        "newest_pr_updated_at": summary_row["newest_pr_updated_at"],
        "pr_states_indexed": summary_row["states"],
        "history_complete": history_complete,
        "older_history_remaining": bool(older_remaining),
        "nested_incomplete_units": nested,
        "evidence_units": units,
        "generation": active.generation if active else 0,
        "checked_at": reg.get("last_checked_at") or reg.get("generation_published_at"),
        "summary": summary,
    }


# ── Querying ──────────────────────────────────────────────────────────────────

def _build_where(where: dict | None, namespace: str | None) -> dict | None:
    where_filter = dict(where or {})
    ns = _normalize_namespace(namespace)
    if ns and "namespace" not in where_filter:
        where_filter["namespace"] = ns
    # ChromaDB requires multiple filters to be wrapped in $and
    if len(where_filter) == 1:
        return where_filter
    if len(where_filter) > 1:
        return {"$and": [{k: v} for k, v in where_filter.items()]}
    return None


def is_bot_metadata(meta: dict) -> bool:
    """True when a unit's metadata marks it as bot-authored (new or legacy keys)."""
    return bool(
        meta.get("is_bot") is True
        or meta.get("author_is_bot") is True
        or meta.get("author_type") == "bot"
    )


def source_kind_of(meta: dict) -> str:
    """Return a unit's source_kind, deriving it for legacy documents."""
    kind = meta.get("source_kind")
    if isinstance(kind, str) and kind:
        return kind
    legacy = {
        "review_comment": "human_review_thread",
        "pr_description": "pr_description",
        "review_summary": "human_review_summary",
        "commit_message": "commit_message",
    }
    derived = legacy.get(str(meta.get("type") or ""), "")
    # Legacy documents predate source_kind: a bot-authored one maps to the bot kind
    # so exclude_bots / source_kinds filters and the projection agree.
    if derived in {"human_review_thread", "human_review_summary"} and is_bot_metadata(meta):
        return derived.replace("human_", "bot_", 1)
    return derived


def _passes_filters(meta: dict, exclude_bots: bool, source_kinds: list[str] | None) -> bool:
    if exclude_bots and is_bot_metadata(meta):
        return False
    if source_kinds and source_kind_of(meta) not in set(source_kinds):
        return False
    return True


def build_where_filter(
    *,
    exclude_bots: bool = False,
    source_kinds: list[str] | None = None,
    extra: dict | None = None,
) -> dict | None:
    """Build a Chroma ``where`` clause for NEW-style documents.

    Chroma excludes documents that lack a filtered key, so legacy documents
    (no ``is_bot``/``source_kind``) would vanish; prefer the ``exclude_bots`` /
    ``source_kinds`` parameters of ``query_similar`` / ``lexical_candidates``,
    which filter in Python and keep legacy documents correct.
    """
    clauses: list[dict] = []
    if exclude_bots:
        clauses.append({"is_bot": False})
    if source_kinds:
        clauses.append({"source_kind": {"$in": list(source_kinds)}})
    for key, value in (extra or {}).items():
        clauses.append({key: value})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def query_similar(
    repo_key: str,
    query_text: str,
    n_results: int = 8,
    temporary: bool = False,
    namespace: str | None = None,
    where: dict | None = None,
    *,
    over_fetch: int = 1,
    exclude_bots: bool = False,
    source_kinds: list[str] | None = None,
    query_embedding: list[float] | None = None,
) -> list[dict]:
    """Semantic search over the ACTIVE generation, with provenance preserved.

    Each result: ``{id, text, metadata, similarity, distance}``.  ``metadata``
    keeps every stored key except the internal ``namespace`` (so ``is_bot``,
    ``source_url``, ``files`` etc. survive).  ``where`` is a Chroma filter.
    ``exclude_bots`` / ``source_kinds`` are applied BEFORE the final cut: the
    candidate pool is ``n_results * over_fetch`` and grows until enough
    candidates pass the filters (or the collection is exhausted).
    """
    embedding = query_embedding if query_embedding is not None else encode(query_text)
    with _chroma_lock:
        active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
        if active is None:
            return []
        collection = active.collection
        total = collection.count()
        if total == 0:
            return []
        final_where = _build_where(where, namespace)
        filtering = exclude_bots or bool(source_kinds)
        want = max(int(n_results), 1)
        pool = min(total, want * max(int(over_fetch), 1))
        kept: list[dict] = []
        while True:
            results = collection.query(
                query_embeddings=[embedding],
                n_results=pool,
                where=final_where,
                include=["documents", "metadatas", "distances"],
            )
            ids = (results.get("ids") or [[]])[0]
            docs = (results.get("documents") or [[]])[0]
            if not docs:
                return []
            kept = []
            for doc_id, doc, meta, dist in zip(
                ids, docs, results["metadatas"][0], results["distances"][0]
            ):
                meta = meta or {}
                if filtering and not _passes_filters(meta, exclude_bots, source_kinds):
                    continue
                kept.append(
                    {
                        "id": doc_id,
                        "text": doc,
                        "metadata": {k: v for k, v in meta.items() if k != "namespace"},
                        "similarity": round(1 - dist, 4),
                        "distance": dist,
                    }
                )
            if not filtering or len(kept) >= want or pool >= total or len(docs) < pool:
                break
            pool = min(total, pool * 4)
    return kept[:want]


def get_documents_by_ids(
    repo_key: str,
    ids: list[str],
    temporary: bool = False,
    namespace: str | None = None,
) -> list[dict]:
    """Fetch units by Chroma id from the active generation (request order, no misses)."""
    wanted = [i for i in dict.fromkeys(ids or []) if isinstance(i, str) and i]
    if not wanted:
        return []
    with _chroma_lock:
        active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
        if active is None:
            return []
        data = active.collection.get(ids=wanted, include=["documents", "metadatas"])
    by_id = {
        doc_id: {
            "id": doc_id,
            "text": doc,
            "metadata": {k: v for k, v in (meta or {}).items() if k != "namespace"},
        }
        for doc_id, doc, meta in zip(
            data.get("ids") or [], data.get("documents") or [], data.get("metadatas") or []
        )
    }
    return [by_id[i] for i in wanted if i in by_id]


def get_thread_parts(
    repo_key: str,
    thread_evidence_id: str,
    temporary: bool = False,
    namespace: str | None = None,
) -> list[dict]:
    """Return every part of a (possibly split) unit, ordered by ``part_index``.

    Accepts a ``thread_evidence_id``, the ``evidence_id`` of any part, or the
    id of an unsplit unit (returned alone).  Empty list when nothing matches.
    """
    if not isinstance(thread_evidence_id, str) or not thread_evidence_id:
        return []
    with _chroma_lock:
        active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
        if active is None:
            return []
        collection = active.collection
        direct = collection.get(ids=[thread_evidence_id], include=["documents", "metadatas"])
        thread_id = thread_evidence_id
        direct_rows = list(zip(
            direct.get("ids") or [], direct.get("documents") or [], direct.get("metadatas") or []
        ))
        if direct_rows:
            owner = (direct_rows[0][2] or {}).get("thread_evidence_id")
            if isinstance(owner, str) and owner:
                thread_id = owner
        grouped = collection.get(
            where={"thread_evidence_id": thread_id}, include=["documents", "metadatas"]
        )
    rows = list(zip(
        grouped.get("ids") or [], grouped.get("documents") or [], grouped.get("metadatas") or []
    ))
    if not rows:
        rows = direct_rows
    parts = [
        {
            "id": doc_id,
            "text": doc,
            "metadata": {k: v for k, v in (meta or {}).items() if k != "namespace"},
        }
        for doc_id, doc, meta in rows
    ]
    parts.sort(key=lambda p: (p["metadata"].get("part_index", 0) or 0, p["id"]))
    return parts


# ── Lexical retrieval (hybrid + degraded mode) ────────────────────────────────

_STOPWORDS = frozenset(
    "a an and are as at be but by can did do does for from had has have how i if in into is it its "
    "me my no not of on or our should so than that the their then there these they this to was we "
    "were what when where which who why will with would you your about any all been being over "
    "pr prs pull request requests review reviews code".split()
)
_NOT_IDENTIFIERS = frozenset({"e.g", "i.e", "etc", "vs", "e.g.", "i.e."})
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_IDENT_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:\\#@-]+")
_PR_REF_RE = re.compile(r"(?:\bpr\s*#?\s*|\bpull\s*(?:request)?\s*#?\s*|#)(\d{1,7})\b", re.IGNORECASE)


def extract_query_identifiers(query: str) -> dict:
    """Pull exact-match identifiers out of a query.

    Returns ``{"identifiers": [...], "pr_numbers": [...]}``.  Identifiers are
    backticked spans, paths / dotted / namespaced names (``a/b.py``, ``Foo.bar``,
    ``foo::bar``), snake_case, camelCase and PascalCase tokens.
    """
    text = query or ""
    pr_numbers = sorted({int(m) for m in _PR_REF_RE.findall(text)})
    identifiers: list[str] = []

    def _add(value: str) -> None:
        value = value.strip().strip(".,;:!?()[]{}\"'")
        if len(value) < 3 or value.lower() in _NOT_IDENTIFIERS:
            return
        if value.startswith("#") and value[1:].isdigit():
            return
        if value.lower() not in {i.lower() for i in identifiers}:
            identifiers.append(value)

    for span in re.findall(r"`([^`\n]{2,})`", text):
        _add(span)
    for token in _IDENT_TOKEN_RE.findall(text):
        token = token.strip(".,;:!?-")
        if not token:
            continue
        if re.fullmatch(r"#?\d+", token):
            continue
        has_sep = any(ch in token for ch in "/._:\\@") and re.search(r"[A-Za-z]", token)
        snake = "_" in token and re.search(r"[A-Za-z]", token)
        camel = bool(re.search(r"[a-z][A-Z]", token)) or bool(re.fullmatch(r"[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*", token))
        hyphen_code = "-" in token and re.search(r"\d", token) and re.search(r"[A-Za-z]", token)
        if has_sep or snake or camel or hyphen_code:
            _add(token)
    return {"identifiers": identifiers, "pr_numbers": pr_numbers}


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in _tokenize(query):
        if len(token) < 3 or token in _STOPWORDS or token.isdigit():
            continue
        if token not in terms:
            terms.append(token)
        for piece in token.split("_"):
            if len(piece) >= 3 and piece not in _STOPWORDS and piece not in terms:
                terms.append(piece)
    return terms


def lexical_candidates(
    repo_key: str,
    query_text: str,
    limit: int = 20,
    temporary: bool = False,
    namespace: str | None = None,
    where: dict | None = None,
    *,
    exclude_bots: bool = False,
    source_kinds: list[str] | None = None,
) -> list[dict]:
    """Scan the ACTIVE generation for query-token and exact-identifier matches.

    Usable without the embedding model (degraded mode) and as the exact leg of
    hybrid retrieval.  Each result: ``{id, text, metadata, score, match,
    matched_identifiers, matched_terms}`` where ``match`` is ``"exact"`` when an
    identifier / path / PR reference matched and ``"lexical"`` for token overlap
    only.  Exact matches always rank above token-only matches.
    """
    parsed = extract_query_identifiers(query_text)
    identifiers = [i for i in parsed["identifiers"]]
    pr_numbers = set(parsed["pr_numbers"])
    terms = _query_terms(query_text)
    if not identifiers and not pr_numbers and not terms:
        return []

    final_where = _build_where(where, namespace)
    lowered_ids = [i.lower() for i in identifiers]
    candidates: list[dict] = []
    doc_count = 0
    total_len = 0
    df: Counter = Counter()

    with _chroma_lock:
        active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
        if active is None:
            return []
        for batch in _iter_records(active.collection, ["documents", "metadatas"], final_where):
            for doc_id, doc, meta in zip(
                batch["ids"], batch.get("documents") or [], batch.get("metadatas") or []
            ):
                meta = meta or {}
                if not _passes_filters(meta, exclude_bots, source_kinds):
                    continue
                text = doc or ""
                tokens = _tokenize(text)
                doc_count += 1
                total_len += len(tokens)
                counts = Counter(tokens)
                for term in terms:
                    if counts.get(term):
                        df[term] += 1
                haystack = text.lower()
                path = str(meta.get("original_path") or "").lower()
                url = str(meta.get("source_url") or "").lower()
                matched_ids = [
                    ident
                    for ident, low in zip(identifiers, lowered_ids)
                    if low in haystack or (path and low in path) or (url and low in url)
                ]
                pr_hit = meta.get("pr_number") in pr_numbers if pr_numbers else False
                matched_terms = [t for t in terms if counts.get(t)]
                if not matched_ids and not pr_hit and not matched_terms:
                    continue
                candidates.append(
                    {
                        "id": doc_id,
                        "text": text,
                        "metadata": {k: v for k, v in meta.items() if k != "namespace"},
                        "_counts": {t: counts[t] for t in matched_terms},
                        "_len": len(tokens),
                        "matched_identifiers": matched_ids,
                        "matched_terms": matched_terms,
                        "_pr_hit": pr_hit,
                    }
                )

    if not candidates:
        return []
    avg_len = (total_len / doc_count) if doc_count else 1.0
    k1, b = 1.5, 0.75
    results: list[dict] = []
    for cand in candidates:
        bm25 = 0.0
        for term, tf in cand.pop("_counts").items():
            idf = math.log(1 + (doc_count - df[term] + 0.5) / (df[term] + 0.5))
            norm = tf + k1 * (1 - b + b * (cand["_len"] / (avg_len or 1.0)))
            bm25 += idf * (tf * (k1 + 1)) / norm
        cand.pop("_len")
        pr_hit = cand.pop("_pr_hit")
        exact_strength = len(cand["matched_identifiers"]) + (1 if pr_hit else 0)
        cand["match"] = "exact" if exact_strength else "lexical"
        cand["score"] = round(exact_strength * 100.0 + bm25, 4)
        results.append(cand)
    results.sort(key=lambda r: (-r["score"], r["id"]))
    return results[: max(int(limit), 1)]


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_collection_stats(
    repo_key: str,
    temporary: bool = False,
    namespace: str | None = None,
) -> dict:
    storage = "temporary" if temporary else "permanent"
    ns = _normalize_namespace(namespace)
    with _chroma_lock:
        active = _resolve_active(repo_key, temporary=temporary, namespace=namespace)
        reg = _registry().get_registry(repo_key, namespace=namespace, storage=storage)
        count = 0
        meta: dict = {}
        if active is not None:
            where_filter = {"namespace": ns} if ns else None
            try:
                data = active.collection.get(where=where_filter, include=[])
                count = len(data["ids"]) if data and "ids" in data else 0
            except Exception:
                count = 0
            meta = active.collection.metadata or {}

        last_indexed = reg.get("generation_published_at") or meta.get("last_indexed_at")
        last_checked = reg.get("last_checked_at") or meta.get("last_checked_at") or last_indexed

        is_stale = False
        days_old = None
        if last_checked:
            try:
                last_dt = datetime.datetime.fromisoformat(last_checked)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
                delta = datetime.datetime.now(datetime.timezone.utc) - last_dt
                days_old = delta.days
                is_stale = days_old > 30
            except Exception:
                pass

    return {
        "repo": repo_key,
        "namespace": ns,
        "total_documents": count,
        "storage": storage,
        "last_indexed_at": last_indexed,
        "last_checked_at": last_checked,
        "days_old": days_old,
        "is_stale": is_stale,
        "generation": active.generation if active else 0,
        "legacy_index": bool(active.legacy) if active else False,
        "history_complete": reg.get("history_complete"),
        "backfill_pending": bool(reg.get("backfill_cursor")),
        "refresh_pending": bool(reg.get("refresh_cursor")),
        "last_job_status": reg.get("last_job_status"),
        "last_job_error_kind": reg.get("last_job_error_kind"),
        "last_job_finished_at": reg.get("last_job_finished_at"),
    }


# ── Deletion ──────────────────────────────────────────────────────────────────

def _delete_scope_collections(client, repo_key: str, namespace: str | None) -> bool:
    deleted = False
    for name in _existing_generations(client, repo_key, namespace).values():
        deleted = _safe_delete_collection(client, name) or deleted
    deleted = _safe_delete_collection(client, _collection_name(repo_key, namespace)) or deleted
    return deleted


def delete_repo_index(
    repo_key: str,
    storage: str = "both",
    namespace: str | None = None,
) -> dict:
    """Delete every generation, the registry row and the PR ledger for a scope."""
    if storage not in {"temporary", "permanent", "both"}:
        raise ValueError("storage must be one of: temporary, permanent, both")

    ns = _normalize_namespace(namespace)

    deleted = {"temporary": False, "permanent": False}

    with _chroma_lock:
        if storage in {"temporary", "both"}:
            key = _index_scope_key(repo_key, True, namespace)
            _INDEX_EPOCHS[key] = _INDEX_EPOCHS.get(key, 0) + 1
            deleted["temporary"] = _delete_scope_collections(_ephemeral_client, repo_key, namespace)

        if storage in {"permanent", "both"}:
            key = _index_scope_key(repo_key, False, namespace)
            _INDEX_EPOCHS[key] = _INDEX_EPOCHS.get(key, 0) + 1
            deleted["permanent"] = _delete_scope_collections(_persistent_client, repo_key, namespace)

        registry = _registry()
        if storage in {"temporary", "both"}:
            registry.clear_cursor(repo_key, namespace=namespace, storage="temporary")
        if storage in {"permanent", "both"}:
            registry.clear_cursor(repo_key, namespace=namespace, storage="permanent")

    return {
        "repo": repo_key,
        "namespace": ns,
        "storage": storage,
        "deleted": deleted,
        "deleted_any": any(deleted.values()),
    }
