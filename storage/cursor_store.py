import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class CursorStore:
    """SQLite watermarks scoped to repository, namespace, and storage mode."""

    def __init__(self, db_path: str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)

    def _init_db(self):
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
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
            # One row per PR actually imported. Refreshes re-fetch an overlap
            # window, so counts and ranges are derived from distinct PRs here
            # rather than accumulated, which would double count.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS indexed_prs (
                    repo_key TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    storage TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    updated_at TEXT,
                    PRIMARY KEY (repo_key, namespace, storage, pr_number)
                )
            ''')
            existing_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(index_cursors)")
            }
            for column, definition in {
                "refresh_cursor": "TEXT",
                "refresh_high_watermark": "TEXT",
                # How far the last full import was allowed to reach, and whether
                # it stopped short of the repository's oldest PR. NULL means
                # "never recorded" (an index built before bounds existed).
                "configured_pr_limit": "INTEGER",
                "configured_pages": "INTEGER",
                "history_truncated": "INTEGER",
            }.items():
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE index_cursors ADD COLUMN {column} {definition}"
                    )

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
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
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
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
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
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
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
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
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
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
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
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
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

    def record_indexed_prs(
        self,
        repo_key: str,
        prs: list[dict],
        *,
        namespace: str | None = None,
        storage: str = "permanent",
        pr_limit: int | None = None,
        pages: int | None = None,
        history_truncated: bool | None = None,
    ) -> None:
        """Remember which PRs an import covered, and how far it was allowed to reach.

        ``pr_limit``/``pages``/``history_truncated`` describe a full import; pass
        ``None`` (an incremental refresh) to leave the earlier values untouched.
        """
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        rows = [
            (repo_key, ns, storage, pr["number"], pr.get("updated_at") or None)
            for pr in prs
            if isinstance(pr.get("number"), int)
        ]
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO index_cursors (repo_key, namespace, storage)
                       VALUES (?, ?, ?) ON CONFLICT(repo_key, namespace, storage) DO NOTHING""",
                    (repo_key, ns, storage),
                )
                conn.executemany(
                    """INSERT INTO indexed_prs (repo_key, namespace, storage, pr_number, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(repo_key, namespace, storage, pr_number)
                       DO UPDATE SET updated_at = excluded.updated_at""",
                    rows,
                )
                if history_truncated is not None:
                    conn.execute(
                        """UPDATE index_cursors
                           SET configured_pr_limit = ?, configured_pages = ?, history_truncated = ?
                           WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                        (pr_limit, pages, int(history_truncated), repo_key, ns, storage),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def get_bounds(
        self,
        repo_key: str,
        namespace: str | None = None,
        storage: str = "permanent",
    ) -> dict | None:
        """Summarize what the stored PR history covers; ``None`` if never recorded."""
        ns = self._namespace_key(namespace)
        storage = self._validate_storage(storage)
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
            count, oldest_number, newest_number, oldest_at, newest_at = conn.execute(
                """SELECT COUNT(*), MIN(pr_number), MAX(pr_number),
                          MIN(updated_at), MAX(updated_at)
                   FROM indexed_prs
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            ).fetchone()
            row = conn.execute(
                """SELECT configured_pr_limit, configured_pages, history_truncated
                   FROM index_cursors
                   WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                (repo_key, ns, storage),
            ).fetchone()
        if count == 0:
            return None
        limit, pages, truncated = row if row else (None, None, None)
        return {
            "recorded": True,
            "indexed_pr_count": count,
            "oldest_pr_number": oldest_number,
            "newest_pr_number": newest_number,
            "oldest_pr_updated_at": oldest_at,
            "newest_pr_updated_at": newest_at,
            "configured_pr_limit": limit,
            "configured_pages": pages,
            "history_truncated": None if truncated is None else bool(truncated),
        }

    def migrate_legacy_cursors(self, *, dry_run: bool = False) -> dict[str, int | bool]:
        """Copy pre-v3 cursor rows without treating local timestamps as GitHub watermarks."""
        report: dict[str, int | bool] = {
            "dry_run": dry_run,
            "migrated": 0,
            "skipped": 0,
        }
        import contextlib

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
        import contextlib

        with contextlib.closing(self._get_conn()) as conn:
            for table in ("index_cursors", "indexed_prs"):
                conn.execute(
                    f"""DELETE FROM {table}
                        WHERE repo_key = ? AND namespace = ? AND storage = ?""",
                    (repo_key, ns, storage),
                )


_DEFAULT_DB_DIR = os.path.join(os.path.expanduser("~"), ".github-pr-mcp")
CURSOR_DB_PATH = os.getenv("CURSOR_DB_PATH", os.path.join(_DEFAULT_DB_DIR, "cursors.db"))
cursor_store = CursorStore(CURSOR_DB_PATH)
