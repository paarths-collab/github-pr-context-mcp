import contextlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_UNSET = object()

_REGISTRY_COLUMNS = {
    "refresh_cursor": "TEXT",
    "refresh_high_watermark": "TEXT",
    # Registry: active generation + publication/check times.
    "active_generation": "INTEGER DEFAULT 0",
    "generation_published_at": "TEXT",
    "last_checked_at": "TEXT",
    "refresh_applied": "INTEGER DEFAULT 0",
    # Older-history backfill state (history_complete: NULL unknown, 0/1).
    "backfill_cursor": "TEXT",
    "history_complete": "INTEGER",
    "backfill_pages_done": "INTEGER DEFAULT 0",
    # Last indexing job outcome.
    "last_job_status": "TEXT",
    "last_job_error_kind": "TEXT",
    "last_job_finished_at": "TEXT",
}


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the registry schema; tolerant of pre-existing files."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS index_cursors (
            repo_key TEXT NOT NULL,
            namespace TEXT NOT NULL,
            storage TEXT NOT NULL,
            last_pr_number INTEGER DEFAULT 0,
            last_indexed_at TEXT,
            last_updated_at TEXT,
            refresh_cursor TEXT,
            refresh_high_watermark TEXT,
            PRIMARY KEY (repo_key, namespace, storage)
        )
    ''')
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(index_cursors)")}
    for column, definition in _REGISTRY_COLUMNS.items():
        if column not in existing_columns:
            try:
                conn.execute(f"ALTER TABLE index_cursors ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                # Another process migrated the file between the PRAGMA and ALTER.
                refreshed = {row[1] for row in conn.execute("PRAGMA table_info(index_cursors)")}
                if column not in refreshed:
                    raise
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pr_ledger (
            repo_key TEXT NOT NULL,
            namespace TEXT NOT NULL,
            storage TEXT NOT NULL,
            generation INTEGER NOT NULL,
            pr_number INTEGER NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            state TEXT,
            nested_incomplete_threads INTEGER DEFAULT 0,
            PRIMARY KEY (repo_key, namespace, storage, generation, pr_number)
        )
    ''')


class CursorStore:
    """Registry of index state scoped to repository, namespace, and storage mode.

    Permanent scopes live in the SQLite file at ``db_path``. Temporary scopes
    live ONLY in a process-local in-memory SQLite database: temporary Chroma
    data is ephemeral, so its registry/cursors must never outlive the process.
    """

    def __init__(self, db_path: str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._mem_lock = threading.RLock()
        self._mem_conn = sqlite3.connect(
            ":memory:", isolation_level=None, check_same_thread=False
        )
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)

    @contextlib.contextmanager
    def _conn(self, storage: str = "permanent"):
        """Yield the connection for a storage mode (memory for temporary)."""
        if storage == "temporary":
            with self._mem_lock:
                yield self._mem_conn
        else:
            with contextlib.closing(self._get_conn()) as conn:
                yield conn

    def _init_db(self):
        with contextlib.closing(self._get_conn()) as conn:
            _create_schema(conn)
            # Older versions persisted temporary rows on disk. The matching
            # ephemeral Chroma data is gone after a restart, so a surviving
            # cursor would make a refresh skip history that no longer exists.
            conn.execute("DELETE FROM index_cursors WHERE storage = 'temporary'")
            conn.execute("DELETE FROM pr_ledger WHERE storage = 'temporary'")
        with self._mem_lock:
            _create_schema(self._mem_conn)

    @staticmethod
    def _namespace_key(namespace: str | None) -> str:
        normalized = namespace.strip() if isinstance(namespace, str) else ""
        return "default:" if not normalized else f"namespace:{normalized}"

    @staticmethod
    def _validate_storage(storage: str) -> str:
        if storage not in {"permanent", "temporary"}:
            raise ValueError("storage must be permanent or temporary")
        return storage

    def get_cursor(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
    ) -> int:
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        with self._conn(storage) as conn:
            row = conn.execute(
                """SELECT last_pr_number FROM index_cursors
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            ).fetchone()
            return row[0] if row else 0

    def get_updated_at(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
    ) -> str | None:
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        with self._conn(storage) as conn:
            row = conn.execute(
                """SELECT last_updated_at FROM index_cursors
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            ).fetchone()
            return row[0] if row and row[0] else None

    def get_refresh_state(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
    ) -> dict[str, str | None]:
        """Read the durable continuation state for a capped incremental refresh."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        with self._conn(storage) as conn:
            row = conn.execute(
                """SELECT last_updated_at, refresh_cursor, refresh_high_watermark
                   FROM index_cursors
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            ).fetchone()
        if row is None:
            return {
                "last_updated_at": None,
                "refresh_cursor": None,
                "refresh_high_watermark": None,
            }
        return {
            "last_updated_at": row[0] or None,
            "refresh_cursor": row[1] or None,
            "refresh_high_watermark": row[2] or None,
        }

    def set_cursor(
        self,
        repo_key: str,
        last_pr_number: int,
        namespace: str | None = None,
        storage: str = "permanent",
        last_updated_at: str | None = None,
    ):
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn(storage) as conn:
            conn.execute('''
                INSERT INTO index_cursors (
                    repo_key, namespace, storage, last_pr_number, last_indexed_at, last_updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_key, namespace, storage) DO UPDATE SET
                    last_pr_number = MAX(last_pr_number, excluded.last_pr_number),
                    last_indexed_at = excluded.last_indexed_at,
                    last_updated_at = CASE
                        WHEN excluded.last_updated_at IS NULL THEN index_cursors.last_updated_at
                        WHEN index_cursors.last_updated_at IS NULL THEN excluded.last_updated_at
                        WHEN excluded.last_updated_at > index_cursors.last_updated_at THEN excluded.last_updated_at
                        ELSE index_cursors.last_updated_at
                    END
            ''', (repo_key, ns, storage, last_pr_number, now_str, last_updated_at))
            conn.commit()

    def checkpoint_refresh(
        self,
        repo_key: str,
        *,
        namespace: str | None = None,
        storage: str = "permanent",
        next_cursor: str,
        high_watermark: str | None,
    ) -> None:
        """Persist a capped refresh without advancing its completed watermark."""
        if not isinstance(next_cursor, str) or not next_cursor:
            raise ValueError("next_cursor is required for a partial refresh")
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        with self._conn(storage) as conn:
            conn.execute(
                '''
                INSERT INTO index_cursors (
                    repo_key, namespace, storage, last_pr_number, last_indexed_at,
                    last_updated_at, refresh_cursor, refresh_high_watermark
                )
                VALUES (?, ?, ?, 0, NULL, NULL, ?, ?)
                ON CONFLICT(repo_key, namespace, storage) DO UPDATE SET
                    refresh_cursor = excluded.refresh_cursor,
                    refresh_high_watermark = CASE
                        WHEN excluded.refresh_high_watermark IS NULL THEN index_cursors.refresh_high_watermark
                        WHEN index_cursors.refresh_high_watermark IS NULL THEN excluded.refresh_high_watermark
                        WHEN excluded.refresh_high_watermark > index_cursors.refresh_high_watermark THEN excluded.refresh_high_watermark
                        ELSE index_cursors.refresh_high_watermark
                    END
                ''',
                (repo_key, ns, storage, next_cursor, high_watermark),
            )

    def complete_refresh(
        self,
        repo_key: str,
        last_pr_number: int,
        *,
        namespace: str | None = None,
        storage: str = "permanent",
        high_watermark: str | None,
    ) -> None:
        """Commit a fully paginated refresh and clear its continuation state."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn(storage) as conn:
            conn.execute(
                '''
                INSERT INTO index_cursors (
                    repo_key, namespace, storage, last_pr_number, last_indexed_at,
                    last_updated_at, refresh_cursor, refresh_high_watermark
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(repo_key, namespace, storage) DO UPDATE SET
                    last_pr_number = MAX(last_pr_number, excluded.last_pr_number),
                    last_indexed_at = excluded.last_indexed_at,
                    last_updated_at = CASE
                        WHEN excluded.last_updated_at IS NULL THEN index_cursors.last_updated_at
                        WHEN index_cursors.last_updated_at IS NULL THEN excluded.last_updated_at
                        WHEN excluded.last_updated_at > index_cursors.last_updated_at THEN excluded.last_updated_at
                        ELSE index_cursors.last_updated_at
                    END,
                    refresh_cursor = NULL,
                    refresh_high_watermark = NULL
                ''',
                (repo_key, ns, storage, last_pr_number, now_str, high_watermark),
            )
            conn.execute(
                """UPDATE index_cursors SET refresh_applied = 1
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            )

    def migrate_legacy_cursors(self, *, dry_run: bool = False) -> dict[str, int | bool]:
        """Copy pre-v3 cursor rows without treating local timestamps as GitHub watermarks."""
        report: dict[str, int | bool] = {
            "dry_run": dry_run,
            "migrated": 0,
            "skipped": 0,
        }
        with contextlib.closing(self._get_conn()) as conn:
            legacy_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cursors'"
            ).fetchone()
            if not legacy_table:
                return report
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cursors)")}
            required = {"repo_key", "namespace", "last_pr_number", "last_indexed_at"}
            if not required.issubset(columns):
                report["skipped"] = 1
                return report
            rows = conn.execute(
                "SELECT repo_key, namespace, last_pr_number, last_indexed_at FROM cursors"
            ).fetchall()
            for repo_key, legacy_namespace, last_pr_number, last_indexed_at in rows:
                if not isinstance(repo_key, str) or not repo_key:
                    report["skipped"] += 1
                    continue
                # v2 conflated None, blank, and the literal '_default'. Preserve
                # that old behavior only in the default v3 scope; never create a
                # new literal '_default' scope from ambiguous legacy data.
                legacy_ns = (
                    legacy_namespace.strip()
                    if isinstance(legacy_namespace, str)
                    else ""
                )
                namespace = None if legacy_ns in {"", "_default"} else legacy_ns
                ns = self._namespace_key(namespace)
                if not dry_run:
                    conn.execute(
                        '''
                        INSERT INTO index_cursors (
                            repo_key, namespace, storage, last_pr_number,
                            last_indexed_at, last_updated_at,
                            refresh_cursor, refresh_high_watermark
                        )
                        VALUES (?, ?, 'permanent', ?, ?, NULL, NULL, NULL)
                        ON CONFLICT(repo_key, namespace, storage) DO UPDATE SET
                            last_pr_number = MAX(last_pr_number, excluded.last_pr_number),
                            last_indexed_at = CASE
                                WHEN index_cursors.last_indexed_at IS NULL THEN excluded.last_indexed_at
                                ELSE index_cursors.last_indexed_at
                            END
                        ''',
                        (
                            repo_key,
                            ns,
                            int(last_pr_number or 0),
                            last_indexed_at if isinstance(last_indexed_at, str) else None,
                        ),
                    )
                report["migrated"] += 1
        return report

    def clear_cursor(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
    ) -> None:
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        with self._conn(storage) as conn:
            conn.execute(
                """DELETE FROM index_cursors
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            )
            conn.execute(
                """DELETE FROM pr_ledger
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            )

    # ── Registry (generations, backfill state, last job outcome) ──────────

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _ensure_row(conn: sqlite3.Connection, key: tuple[str, str, str]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO index_cursors
               (repo_key, namespace, storage, last_pr_number) VALUES (?, ?, ?, 0)""",
            key,
        )

    def get_registry(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
    ) -> dict:
        """Return every registry field for a scope (defaults when no row exists)."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        with self._conn(storage) as conn:
            cur = conn.execute(
                """SELECT last_pr_number, last_indexed_at, last_updated_at,
                          refresh_cursor, refresh_high_watermark,
                          active_generation, generation_published_at, last_checked_at,
                          refresh_applied, backfill_cursor, history_complete,
                          backfill_pages_done, last_job_status, last_job_error_kind,
                          last_job_finished_at
                   FROM index_cursors
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "exists": False,
                "last_pr_number": 0,
                "last_indexed_at": None,
                "last_updated_at": None,
                "refresh_cursor": None,
                "refresh_high_watermark": None,
                "active_generation": 0,
                "generation_published_at": None,
                "last_checked_at": None,
                "refresh_applied": False,
                "backfill_cursor": None,
                "history_complete": None,
                "backfill_pages_done": 0,
                "last_job_status": None,
                "last_job_error_kind": None,
                "last_job_finished_at": None,
            }
        history = row[10]
        return {
            "exists": True,
            "last_pr_number": row[0] or 0,
            "last_indexed_at": row[1] or None,
            "last_updated_at": row[2] or None,
            "refresh_cursor": row[3] or None,
            "refresh_high_watermark": row[4] or None,
            "active_generation": row[5] or 0,
            "generation_published_at": row[6] or None,
            "last_checked_at": row[7] or None,
            "refresh_applied": bool(row[8]),
            "backfill_cursor": row[9] or None,
            "history_complete": None if history is None else bool(history),
            "backfill_pages_done": row[11] or 0,
            "last_job_status": row[12] or None,
            "last_job_error_kind": row[13] or None,
            "last_job_finished_at": row[14] or None,
        }

    def mark_checked(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
        checked_at: str | None = None,
    ) -> None:
        """Record a successful GitHub sync, even when it produced no documents."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        key = (repo_key, ns, storage)
        with self._conn(storage) as conn:
            self._ensure_row(conn, key)
            conn.execute(
                """UPDATE index_cursors SET last_checked_at = ?
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (checked_at or self._now(), *key),
            )

    def publish_generation(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
        *,
        generation: int,
        previous_generation: int = 0,
        ledger_base_generation: int | None = None,
        ledger_upserts: list[dict] | None = None,
        ledger_removals: list[int] | None = None,
        published_at: str | None = None,
        refresh_applied: bool | None = None,
    ) -> None:
        """Atomically make ``generation`` the active one.

        In ONE transaction: verify the active generation is still
        ``previous_generation``, carry the previous ledger forward, apply ledger
        upserts/removals, flip ``active_generation`` and prune ledger rows of
        generations older than the previous one.
        """
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        key = (repo_key, ns, storage)
        published = published_at or self._now()
        base_generation = (
            previous_generation if ledger_base_generation is None else ledger_base_generation
        )
        with self._conn(storage) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_row(conn, key)
                row = conn.execute(
                    """SELECT active_generation FROM index_cursors
                       WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                    key,
                ).fetchone()
                current = (row[0] or 0) if row else 0
                if current != previous_generation:
                    raise RuntimeError(
                        f"Active generation changed from {previous_generation} to "
                        f"{current} while staging; refusing to publish generation {generation}."
                    )
                if generation <= current:
                    raise ValueError("generation must be greater than the active generation")
                conn.execute(
                    """INSERT OR REPLACE INTO pr_ledger
                       (repo_key, namespace, storage, generation, pr_number, created_at,
                        updated_at, state, nested_incomplete_threads)
                       SELECT repo_key, namespace, storage, ?, pr_number, created_at,
                              updated_at, state, nested_incomplete_threads
                       FROM pr_ledger
                       WHERE repo_key = ? AND namespace = ? AND storage = ? AND generation = ?""",
                    (generation, *key, base_generation),
                )
                for number in ledger_removals or []:
                    conn.execute(
                        """DELETE FROM pr_ledger WHERE repo_key = ? AND namespace = ?
                           AND storage = ? AND generation = ? AND pr_number = ?""",
                        (*key, generation, int(number)),
                    )
                for item in ledger_upserts or []:
                    conn.execute(
                        """INSERT OR REPLACE INTO pr_ledger
                           (repo_key, namespace, storage, generation, pr_number, created_at,
                            updated_at, state, nested_incomplete_threads)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            *key,
                            generation,
                            int(item["pr_number"]),
                            item.get("created_at") or "",
                            item.get("updated_at") or "",
                            item.get("state") or "",
                            int(item.get("nested_incomplete_threads") or 0),
                        ),
                    )
                conn.execute(
                    """DELETE FROM pr_ledger WHERE repo_key = ? AND namespace = ?
                       AND storage = ? AND generation NOT IN (?, ?)""",
                    (*key, generation, previous_generation),
                )
                conn.execute(
                    """UPDATE index_cursors SET
                           active_generation = ?, generation_published_at = ?,
                           last_indexed_at = ?, last_checked_at = ?,
                           refresh_applied = CASE WHEN ? IS NULL THEN refresh_applied ELSE ? END
                       WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                    (
                        generation,
                        published,
                        published,
                        published,
                        None if refresh_applied is None else int(bool(refresh_applied)),
                        None if refresh_applied is None else int(bool(refresh_applied)),
                        *key,
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def get_ledger(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
        generation: int | None = None,
    ) -> list[dict]:
        """Return PR ledger rows for a generation (default: the active one)."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        if generation is None:
            generation = self.get_registry(repo_key, namespace, storage)["active_generation"]
        with self._conn(storage) as conn:
            rows = conn.execute(
                """SELECT pr_number, created_at, updated_at, state, nested_incomplete_threads
                   FROM pr_ledger
                   WHERE repo_key = ? AND namespace = ? AND storage = ? AND generation = ?
                   ORDER BY pr_number""",
                (repo_key, ns, storage, generation),
            ).fetchall()
        return [
            {
                "pr_number": r[0],
                "created_at": r[1] or "",
                "updated_at": r[2] or "",
                "state": r[3] or "",
                "nested_incomplete_threads": r[4] or 0,
            }
            for r in rows
        ]

    def ledger_summary(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
        generation: int | None = None,
    ) -> dict:
        """Aggregate ledger statistics for a generation (default: the active one)."""
        rows = self.get_ledger(repo_key, namespace, storage, generation)
        created = sorted(r["created_at"] for r in rows if r["created_at"])
        updated = sorted(r["updated_at"] for r in rows if r["updated_at"])
        numbers = [r["pr_number"] for r in rows]
        return {
            "count": len(rows),
            "pr_number_min": min(numbers) if numbers else None,
            "pr_number_max": max(numbers) if numbers else None,
            "oldest_pr_created_at": created[0] if created else None,
            "newest_pr_created_at": created[-1] if created else None,
            "newest_pr_updated_at": updated[-1] if updated else None,
            "states": sorted({r["state"] for r in rows if r["state"]}),
            "nested_incomplete_threads": sum(r["nested_incomplete_threads"] for r in rows),
        }

    def set_backfill_state(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
        *,
        backfill_cursor=_UNSET,
        history_complete=_UNSET,
        pages_done=_UNSET,
        add_pages: int = 0,
    ) -> None:
        """Persist older-history backfill progress; never touches refresh state."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        key = (repo_key, ns, storage)
        sets: list[str] = []
        params: list = []
        if backfill_cursor is not _UNSET:
            sets.append("backfill_cursor = ?")
            params.append(backfill_cursor or None)
        if history_complete is not _UNSET:
            sets.append("history_complete = ?")
            params.append(None if history_complete is None else int(bool(history_complete)))
        if pages_done is not _UNSET:
            sets.append("backfill_pages_done = ?")
            params.append(int(pages_done))
        elif add_pages:
            sets.append("backfill_pages_done = COALESCE(backfill_pages_done, 0) + ?")
            params.append(int(add_pages))
        with self._conn(storage) as conn:
            self._ensure_row(conn, key)
            if sets:
                conn.execute(
                    f"UPDATE index_cursors SET {', '.join(sets)} "
                    "WHERE repo_key = ? AND namespace = ? AND storage = ?",
                    (*params, *key),
                )

    def record_job_outcome(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
        *,
        status: str,
        error_kind: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Record how the most recent indexing job for this scope ended."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        key = (repo_key, ns, storage)
        with self._conn(storage) as conn:
            self._ensure_row(conn, key)
            conn.execute(
                """UPDATE index_cursors SET last_job_status = ?, last_job_error_kind = ?,
                          last_job_finished_at = ?
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (status, error_kind, finished_at or self._now(), *key),
            )


_DEFAULT_DB_DIR = os.path.join(os.path.expanduser("~"), ".github-pr-mcp")
CURSOR_DB_PATH = os.getenv("CURSOR_DB_PATH", os.path.join(_DEFAULT_DB_DIR, "cursors.db"))
cursor_store = CursorStore(CURSOR_DB_PATH)
