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


def test_cursor_store_migrates_registry_and_ledger_columns_on_existing_database():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        seed_conn = sqlite3.connect(path)
        with seed_conn as conn:
            conn.execute(
                """CREATE TABLE index_cursors (
                    repo_key TEXT NOT NULL, namespace TEXT NOT NULL, storage TEXT NOT NULL,
                    last_pr_number INTEGER DEFAULT 0, last_indexed_at TEXT, last_updated_at TEXT,
                    refresh_cursor TEXT, refresh_high_watermark TEXT,
                    PRIMARY KEY (repo_key, namespace, storage)
                )"""
            )
            conn.execute(
                "INSERT INTO index_cursors (repo_key, namespace, storage, last_pr_number, last_updated_at)"
                " VALUES ('acme/widget', 'default:', 'permanent', 7, '2024-01-01T00:00:00Z')"
            )
        seed_conn.close()

        store = CursorStore(path)
        store2 = CursorStore(path)  # re-opening an already-migrated file is tolerated
        check_conn = store._get_conn()
        try:
            columns = {row[1] for row in check_conn.execute("PRAGMA table_info(index_cursors)")}
            tables = {r[0] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check_conn.close()

        assert {
            "active_generation", "generation_published_at", "last_checked_at",
            "backfill_cursor", "history_complete", "backfill_pages_done",
            "last_job_status", "last_job_error_kind", "last_job_finished_at",
        }.issubset(columns)
        assert "pr_ledger" in tables
        registry = store2.get_registry("acme/widget")
        assert registry["active_generation"] == 0
        assert registry["history_complete"] is None
        assert registry["last_pr_number"] == 7
        assert store.get_updated_at("acme/widget") == "2024-01-01T00:00:00Z"
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_registry_backfill_and_job_state_roundtrip_and_are_independent_of_refresh_state():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        store = CursorStore(path)
        repo = "acme/widget"
        store.complete_refresh(repo, 5, high_watermark="2024-05-03T12:00:00Z")

        store.set_backfill_state(repo, backfill_cursor="cur-1", history_complete=False, add_pages=2)
        store.set_backfill_state(repo, add_pages=3)
        state = store.get_registry(repo)
        assert state["backfill_cursor"] == "cur-1"
        assert state["history_complete"] is False
        assert state["backfill_pages_done"] == 5
        # Backfill never moves the refresh watermark.
        assert state["last_updated_at"] == "2024-05-03T12:00:00Z"

        store.set_backfill_state(repo, backfill_cursor=None, history_complete=True)
        state = store.get_registry(repo)
        assert state["backfill_cursor"] is None and state["history_complete"] is True

        store.record_job_outcome(
            repo, status="failed", error_kind="rate_limited", finished_at="2024-06-01T00:00:00Z"
        )
        state = store.get_registry(repo)
        assert (state["last_job_status"], state["last_job_error_kind"]) == ("failed", "rate_limited")
        assert state["last_job_finished_at"] == "2024-06-01T00:00:00Z"
        # A refresh checkpoint leaves backfill state alone.
        store.checkpoint_refresh(repo, next_cursor="r", high_watermark="2024-05-04T00:00:00Z")
        assert store.get_registry(repo)["backfill_cursor"] is None
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_publish_generation_is_atomic_and_carries_the_ledger_forward():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        store = CursorStore(path)
        repo = "acme/widget"
        row = lambda n, created: {  # noqa: E731
            "pr_number": n, "created_at": created, "updated_at": created, "state": "MERGED",
        }
        store.publish_generation(
            repo, generation=1, previous_generation=0,
            ledger_upserts=[row(1, "2024-01-01T00:00:00Z"), row(2, "2024-01-02T00:00:00Z")],
        )
        store.publish_generation(
            repo, generation=2, previous_generation=1, ledger_upserts=[row(3, "2024-01-03T00:00:00Z")],
        )
        assert store.get_registry(repo)["active_generation"] == 2
        assert [r["pr_number"] for r in store.get_ledger(repo)] == [1, 2, 3]
        store.publish_generation(repo, generation=3, previous_generation=2)
        # Only the new and the previous generation keep ledger rows.
        assert store.get_ledger(repo, generation=1) == []
        assert len(store.get_ledger(repo, generation=2)) == 3

        # A stale publisher must not win, and a rejected publish changes nothing.
        with pytest.raises(RuntimeError):
            store.publish_generation(repo, generation=4, previous_generation=2)
        assert store.get_registry(repo)["active_generation"] == 3
        assert len(store.get_ledger(repo)) == 3
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_temporary_registry_is_process_local_and_never_survives_a_restart():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        store = CursorStore(path)
        repo = "acme/widget"
        store.set_cursor(repo, 42, storage="temporary", last_updated_at="2024-05-03T12:00:00Z")
        store.checkpoint_refresh(
            repo, storage="temporary", next_cursor="page-2", high_watermark="2024-05-03T13:00:00Z"
        )
        store.publish_generation(
            repo, storage="temporary", generation=1,
            ledger_upserts=[{"pr_number": 42, "created_at": "2024-05-01T00:00:00Z", "state": "MERGED"}],
        )
        store.set_cursor(repo, 7, storage="permanent")
        assert store.get_cursor(repo, storage="temporary") == 42
        assert store.get_registry(repo, storage="temporary")["active_generation"] == 1

        # Nothing temporary was written to the on-disk database...
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM index_cursors WHERE storage='temporary'").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM pr_ledger WHERE storage='temporary'").fetchone()[0] == 0
        finally:
            conn.close()

        # ...so a restarted process sees no stale temporary cursor while the
        # permanent registry persists.
        restarted = CursorStore(path)
        assert restarted.get_cursor(repo, storage="temporary") == 0
        assert restarted.get_updated_at(repo, storage="temporary") is None
        assert restarted.get_refresh_state(repo, storage="temporary")["refresh_cursor"] is None
        assert restarted.get_registry(repo, storage="temporary")["active_generation"] == 0
        assert restarted.get_ledger(repo, storage="temporary") == []
        assert restarted.get_cursor(repo, storage="permanent") == 7
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_stale_temporary_rows_persisted_by_an_older_version_are_discarded_at_startup():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        legacy = CursorStore(path)
        # Simulate an older release that persisted temporary rows on disk.
        conn = legacy._get_conn()
        try:
            conn.execute(
                "INSERT INTO index_cursors (repo_key, namespace, storage, last_pr_number, last_updated_at)"
                " VALUES ('acme/widget', 'default:', 'temporary', 99, '2024-05-03T12:00:00Z')"
            )
            conn.execute(
                "INSERT INTO index_cursors (repo_key, namespace, storage, last_pr_number)"
                " VALUES ('acme/widget', 'default:', 'permanent', 5)"
            )
        finally:
            conn.close()

        restarted = CursorStore(path)
        assert restarted.get_cursor("acme/widget", storage="temporary") == 0
        assert restarted.get_updated_at("acme/widget", storage="temporary") is None
        assert restarted.get_cursor("acme/widget", storage="permanent") == 5
    finally:
        if os.path.exists(path):
            os.remove(path)
