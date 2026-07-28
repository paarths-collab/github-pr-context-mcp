import pytest
import os
import sqlite3
import tempfile
from storage.cursor_store import CursorStore

def test_cursor_store_roundtrip():
    # Use a temporary file for the database
    fd, path = tempfile.mkstemp()
    os.close(fd)
    
    try:
        store = CursorStore(path)
        repo = "owner/repo"
        ns = "test-ns"
        
        # Initial value should be 0
        assert store.get_cursor(repo, ns) == 0
        
        # Set and get
        store.set_cursor(repo, 123, ns)
        assert store.get_cursor(repo, ns) == 123
        
        # Update with higher value
        store.set_cursor(repo, 125, ns)
        assert store.get_cursor(repo, ns) == 125
        
        # Update with lower value (should keep max)
        store.set_cursor(repo, 120, ns)
        assert store.get_cursor(repo, ns) == 125
        
        # Different namespace
        assert store.get_cursor(repo, "other") == 0
        store.set_cursor(repo, 50, "other")
        assert store.get_cursor(repo, "other") == 50
        assert store.get_cursor(repo, ns) == 125

        # Timestamp watermarks are independent of PR number: an older-numbered
        # PR can be edited after the original index and must trigger a refresh.
        store.set_cursor(repo, 120, ns, last_updated_at="2024-05-03T12:00:00Z")
        assert store.get_updated_at(repo, ns) == "2024-05-03T12:00:00Z"
        store.set_cursor(repo, 119, ns, last_updated_at="2024-05-03T11:00:00Z")
        assert store.get_updated_at(repo, ns) == "2024-05-03T12:00:00Z"
        store.set_cursor(repo, 119, ns, last_updated_at="2024-05-03T12:01:00Z")
        assert store.get_updated_at(repo, ns) == "2024-05-03T12:01:00Z"

        # Each Chroma storage mode has its own index and therefore its own
        # watermark. A permanent refresh must not skip temporary history.
        store.set_cursor(
            repo,
            9,
            ns,
            storage="temporary",
            last_updated_at="2024-05-02T12:00:00Z",
        )
        assert store.get_updated_at(repo, ns, storage="temporary") == "2024-05-02T12:00:00Z"
        assert store.get_updated_at(repo, ns, storage="permanent") == "2024-05-03T12:01:00Z"

        # The literal namespace '_default' must not collide with no namespace.
        store.set_cursor(repo, 1, None, last_updated_at="2024-05-01T00:00:00Z")
        store.set_cursor(repo, 2, "_default", last_updated_at="2024-05-02T00:00:00Z")
        assert store.get_cursor(repo, None) == 1
        assert store.get_cursor(repo, "_default") == 2

        # A capped incremental refresh records only a continuation. The old
        # completed watermark must remain in place until the continuation ends.
        store.checkpoint_refresh(
            repo,
            namespace=ns,
            next_cursor="page-two",
            high_watermark="2024-05-03T12:05:00Z",
        )
        pending = store.get_refresh_state(repo, ns)
        assert pending == {
            "last_updated_at": "2024-05-03T12:01:00Z",
            "refresh_cursor": "page-two",
            "refresh_high_watermark": "2024-05-03T12:05:00Z",
        }

        store.complete_refresh(
            repo,
            126,
            namespace=ns,
            high_watermark="2024-05-03T12:05:00Z",
        )
        assert store.get_updated_at(repo, ns) == "2024-05-03T12:05:00Z"
        assert store.get_refresh_state(repo, ns)["refresh_cursor"] is None
        assert store.get_refresh_state(repo, ns)["refresh_high_watermark"] is None
        
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_cursor_store_explicitly_imports_legacy_rows_without_using_local_time_as_github_watermark():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        seed_conn = sqlite3.connect(path)
        with seed_conn as conn:
            conn.execute(
                """CREATE TABLE cursors (
                    repo_key TEXT, namespace TEXT, last_pr_number INTEGER,
                    last_indexed_at TEXT, PRIMARY KEY (repo_key, namespace)
                )"""
            )
            conn.execute(
                "INSERT INTO cursors VALUES (?, ?, ?, ?)",
                ("acme/widget", "_default", 12, "2024-05-03T12:00:00Z"),
            )
            conn.execute(
                "INSERT INTO cursors VALUES (?, ?, ?, ?)",
                ("acme/widget", "team-a", 15, "2024-05-03T13:00:00Z"),
            )
        seed_conn.close()

        store = CursorStore(path)
        report = store.migrate_legacy_cursors()

        assert report["migrated"] == 2
        assert store.get_cursor("acme/widget", None) == 12
        assert store.get_cursor("acme/widget", "team-a") == 15
        assert store.get_cursor("acme/widget", "_default") == 0
        assert store.get_updated_at("acme/widget", None) is None
        assert store.get_updated_at("acme/widget", "team-a") is None
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_cursor_store_adds_refresh_columns_to_an_existing_v3_database():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        seed_conn = sqlite3.connect(path)
        with seed_conn as conn:
            conn.execute(
                """CREATE TABLE index_cursors (
                    repo_key TEXT NOT NULL, namespace TEXT NOT NULL, storage TEXT NOT NULL,
                    last_pr_number INTEGER DEFAULT 0, last_indexed_at TEXT, last_updated_at TEXT,
                    PRIMARY KEY (repo_key, namespace, storage)
                )"""
            )
        seed_conn.close()

        store = CursorStore(path)
        check_conn = store._get_conn()
        try:
            columns = {row[1] for row in check_conn.execute("PRAGMA table_info(index_cursors)")}
        finally:
            check_conn.close()

        assert {"refresh_cursor", "refresh_high_watermark"}.issubset(columns)
    finally:
        if os.path.exists(path):
            os.remove(path)
