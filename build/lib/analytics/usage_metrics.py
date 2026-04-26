from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

class UsageMetricsStore:
    """Persist and summarize anonymous usage metrics for hosted MCP deployments backed by thread-safe SQLite."""

    def __init__(self, file_path: str):
        # Swap existing json suffixes to .db without breaking integrations
        p = Path(file_path)
        if p.suffix == '.json':
            self._path = p.with_suffix('.db')
        else:
            self._path = p
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        # isolation_level=None enables autocommit for simple operations
        # check_same_thread=False allows sharing across async workers
        return sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS global_stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER DEFAULT 0
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            conn.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('tracked_since', ?)", (_utc_now().isoformat(),))
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS tools (
                    name TEXT PRIMARY KEY,
                    calls INTEGER DEFAULT 0
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_hash TEXT PRIMARY KEY,
                    first_seen TEXT,
                    last_seen TEXT,
                    events INTEGER DEFAULT 0
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_tool_calls (
                    day TEXT PRIMARY KEY,
                    calls INTEGER DEFAULT 0
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_users (
                    day TEXT,
                    user_hash TEXT,
                    calls INTEGER DEFAULT 0,
                    PRIMARY KEY (day, user_hash)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS pings (
                    mode TEXT,
                    user_hash TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    count INTEGER DEFAULT 0,
                    PRIMARY KEY (mode, user_hash)
                )
            ''')

    def _hash_user(self, user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]

    def record_event(self, user_id: str, tool_name: str) -> None:
        user_hash = self._hash_user(user_id)
        now_str = _utc_now().isoformat()
        day = _utc_now().date().isoformat()

        with self._get_conn() as conn:
            conn.execute("INSERT INTO global_stats (key, value) VALUES ('total_tool_calls', 1) ON CONFLICT(key) DO UPDATE SET value = value + 1")
            conn.execute("INSERT INTO tools (name, calls) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET calls = calls + 1", (tool_name,))
            conn.execute("INSERT INTO users (user_hash, first_seen, last_seen, events) VALUES (?, ?, ?, 1) ON CONFLICT(user_hash) DO UPDATE SET last_seen = ?, events = events + 1", (user_hash, now_str, now_str, now_str))
            conn.execute("INSERT INTO daily_tool_calls (day, calls) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET calls = calls + 1", (day,))
            conn.execute("INSERT INTO daily_users (day, user_hash, calls) VALUES (?, ?, 1) ON CONFLICT(day, user_hash) DO UPDATE SET calls = calls + 1", (day, user_hash))
            
    def record_ping(self, anonymous_id: str, mode: str) -> None:
        ALLOWED_MODES = {"render", "uvx", "pipx", "local", "unknown"}
        safe_mode = mode if mode in ALLOWED_MODES else "unknown"
        user_hash = self._hash_user(anonymous_id)
        now_str = _utc_now().isoformat()

        with self._get_conn() as conn:
            conn.execute("INSERT INTO pings (mode, user_hash, first_seen, last_seen, count) VALUES (?, ?, ?, ?, 1) ON CONFLICT(mode, user_hash) DO UPDATE SET last_seen = ?, count = count + 1", (safe_mode, user_hash, now_str, now_str, now_str))

    def update_github_clones(self, count: int) -> None:
        """Update the persistent record of unique github clones fetched from the traffic API."""
        with self._get_conn() as conn:
            conn.execute("INSERT INTO global_stats (key, value) VALUES ('github_clones', ?) ON CONFLICT(key) DO UPDATE SET value = ?", (count, count))

    def summary(self, last_days: int = 30) -> dict[str, Any]:
        last_days = max(1, min(last_days, 365))
        
        with self._get_conn() as conn:
            tracked_since = conn.execute("SELECT value FROM system_config WHERE key = 'tracked_since'").fetchone()
            tracked_since_val = tracked_since[0] if tracked_since else None
            
            total_calls = conn.execute("SELECT value FROM global_stats WHERE key = 'total_tool_calls'").fetchone()
            total_calls_val = total_calls[0] if total_calls else 0
            
            tools_cursor = conn.execute("SELECT name, calls FROM tools ORDER BY calls DESC")
            top_tools = [{"tool": row[0], "calls": row[1]} for row in tools_cursor]
            
            daily_series = []
            days_cursor = conn.execute("SELECT day, calls FROM daily_tool_calls ORDER BY day DESC LIMIT ?", (last_days,))
            days_data = days_cursor.fetchall()
            
            for day, calls in reversed(days_data):
                users_count = conn.execute("SELECT COUNT(DISTINCT user_hash) FROM daily_users WHERE day = ?", (day,)).fetchone()[0]
                daily_series.append({
                    "date": day,
                    "tool_calls": calls,
                    "unique_users": users_count
                })

            users_by_mode: dict[str, int] = {}
            modes_cursor = conn.execute("SELECT mode, COUNT(DISTINCT user_hash) FROM pings GROUP BY mode")
            for mode, count in modes_cursor:
                users_by_mode[mode] = count
                
            total_auth_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if "render" not in users_by_mode or users_by_mode["render"] < total_auth_users:
                users_by_mode["render"] = total_auth_users
                
            github_clones = conn.execute("SELECT value FROM global_stats WHERE key = 'github_clones'").fetchone()
            github_clones_val = github_clones[0] if github_clones else 0
            
            total_ping_users = conn.execute("SELECT COUNT(DISTINCT user_hash) FROM pings").fetchone()[0]
            total_unique = total_ping_users + total_auth_users + github_clones_val
            
        return {
            "tracked_since": tracked_since_val,
            "total_tool_calls": total_calls_val,
            "total_unique_users": total_unique,
            "users_by_mode": users_by_mode,
            "top_tools": top_tools,
            "daily": daily_series,
        }
