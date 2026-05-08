import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone

class CursorStore:
    """
    SQLite-backed store for tracking indexing progress (last PR number).
    This is more reliable than ChromaDB metadata for single-value state.
    """
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
                CREATE TABLE IF NOT EXISTS cursors (
                    repo_key TEXT,
                    namespace TEXT,
                    last_pr_number INTEGER DEFAULT 0,
                    last_indexed_at TEXT,
                    PRIMARY KEY (repo_key, namespace)
                )
            ''')

    def get_cursor(self, repo_key: str, namespace: str | None = None) -> int:
        ns = namespace or "_default"
        import contextlib
        with contextlib.closing(self._get_conn()) as conn:
            row = conn.execute(
                "SELECT last_pr_number FROM cursors WHERE repo_key = ? AND namespace = ?",
                (repo_key, ns)
            ).fetchone()
            return row[0] if row else 0

    def set_cursor(self, repo_key: str, last_pr_number: int, namespace: str | None = None):
        ns = namespace or "_default"
        now_str = datetime.now(timezone.utc).isoformat()
        import contextlib
        with contextlib.closing(self._get_conn()) as conn:
            conn.execute('''
                INSERT INTO cursors (repo_key, namespace, last_pr_number, last_indexed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo_key, namespace) DO UPDATE SET
                    last_pr_number = MAX(last_pr_number, excluded.last_pr_number),
                    last_indexed_at = excluded.last_indexed_at
            ''', (repo_key, ns, last_pr_number, now_str))
            conn.commit()

# Singleton instance
_DEFAULT_DB_DIR = os.path.join(os.path.expanduser("~"), ".github-pr-mcp")
CURSOR_DB_PATH = os.getenv("CURSOR_DB_PATH", os.path.join(_DEFAULT_DB_DIR, "cursors.db"))
cursor_store = CursorStore(CURSOR_DB_PATH)
