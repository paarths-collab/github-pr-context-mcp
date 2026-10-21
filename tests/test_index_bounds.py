"""Index bounds: what an import covered must be stored, and honestly reported."""

import pytest

from storage.coverage import describe_coverage, history_complete
from storage.cursor_store import CursorStore
from fetcher.queries import PR_QUERY, PRS_PER_PAGE


def _prs(*numbers_and_dates):
    return [{"number": n, "updated_at": d} for n, d in numbers_and_dates]


@pytest.fixture
def store(tmp_path):
    return CursorStore(str(tmp_path / "cursors.db"))


def test_full_import_records_count_range_limit_and_truncation(store):
    store.record_indexed_prs(
        "o/r",
        _prs((9, "2024-03-09T00:00:00Z"), (4, "2024-01-02T00:00:00Z"), (7, "2024-02-01T00:00:00Z")),
        pr_limit=60, pages=2, history_truncated=True,
    )
    assert store.get_bounds("o/r") == {
        "recorded": True,
        "indexed_pr_count": 3,
        "oldest_pr_number": 4,
        "newest_pr_number": 9,
        "oldest_pr_updated_at": "2024-01-02T00:00:00Z",
        "newest_pr_updated_at": "2024-03-09T00:00:00Z",
        "configured_pr_limit": 60,
        "configured_pages": 2,
        "history_truncated": True,
    }


def test_refresh_overlap_does_not_double_count_and_keeps_import_facts(store):
    store.record_indexed_prs(
        "o/r", _prs((1, "2024-01-01T00:00:00Z"), (2, "2024-01-02T00:00:00Z")),
        pr_limit=30, pages=1, history_truncated=False,
    )
    # An incremental refresh re-fetches PR 2 (overlap window) and adds PR 3.
    store.record_indexed_prs(
        "o/r", _prs((2, "2024-01-05T00:00:00Z"), (3, "2024-01-06T00:00:00Z"))
    )
    bounds = store.get_bounds("o/r")
    assert bounds["indexed_pr_count"] == 3
    assert (bounds["oldest_pr_number"], bounds["newest_pr_number"]) == (1, 3)
    assert bounds["newest_pr_updated_at"] == "2024-01-06T00:00:00Z"
    # The refresh says nothing about how far back the original import reached.
    assert (bounds["configured_pr_limit"], bounds["configured_pages"]) == (30, 1)
    assert bounds["history_truncated"] is False


def test_bounds_are_scoped_by_namespace_and_storage_and_deleted_with_the_index(store):
    store.record_indexed_prs("o/r", _prs((1, "2024-01-01T00:00:00Z")), namespace="a", history_truncated=False)
    store.record_indexed_prs("o/r", _prs((8, "2024-01-01T00:00:00Z")), storage="temporary", history_truncated=True)
    assert store.get_bounds("o/r", namespace="a")["newest_pr_number"] == 1
    assert store.get_bounds("o/r", storage="temporary")["newest_pr_number"] == 8
    assert store.get_bounds("o/r") is None

    store.clear_cursor("o/r", namespace="a")
    assert store.get_bounds("o/r", namespace="a") is None
    assert store.get_bounds("o/r", storage="temporary") is not None


def test_bounds_survive_reopening_the_database(tmp_path):
    path = str(tmp_path / "cursors.db")
    CursorStore(path).record_indexed_prs(
        "o/r", _prs((5, "2024-05-05T00:00:00Z")), pr_limit=30, pages=1, history_truncated=True
    )
    assert CursorStore(path).get_bounds("o/r")["history_truncated"] is True


def test_database_created_before_bounds_existed_is_upgraded_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE index_cursors (
                   repo_key TEXT NOT NULL, namespace TEXT NOT NULL, storage TEXT NOT NULL,
                   last_pr_number INTEGER DEFAULT 0, last_indexed_at TEXT, last_updated_at TEXT,
                   PRIMARY KEY (repo_key, namespace, storage))"""
        )
        conn.execute("INSERT INTO index_cursors VALUES ('o/r', 'default:', 'permanent', 12, 'x', 'y')")
    store = CursorStore(str(path))
    assert store.get_bounds("o/r") is None  # unknown, not invented
    assert store.get_cursor("o/r") == 12


def test_page_size_constant_matches_the_query():
    assert f"first: {PRS_PER_PAGE}," in PR_QUERY


class TestCoverageWording:
    base = {
        "recorded": True, "indexed_pr_count": 60, "oldest_pr_number": 61,
        "newest_pr_number": 120, "oldest_pr_updated_at": "2024-01-02T00:00:00Z",
        "newest_pr_updated_at": "2024-05-09T00:00:00Z", "configured_pr_limit": 60,
        "configured_pages": 2, "history_truncated": True,
    }

    def test_truncated_history_says_older_prs_were_not_imported(self):
        text = describe_coverage(self.base)
        assert text.startswith("PARTIAL HISTORY")
        assert "60 PRs indexed (#61 to #120" in text
        assert "2024-01-02 to 2024-05-09" in text
        assert "limit 60 PRs (2 pages)" in text
        assert "NOT imported" in text
        assert history_complete(self.base) is False

    def test_complete_history_is_still_scoped_to_merged_and_closed_prs(self):
        bounds = {**self.base, "history_truncated": False}
        text = describe_coverage(bounds)
        assert "PARTIAL" not in text
        assert "merged and closed pull requests only" in text
        assert history_complete(bounds) is True

    def test_unrecorded_bounds_are_reported_as_unknown_not_complete(self):
        assert "not recorded" in describe_coverage(None)
        assert history_complete(None) is None
        legacy = {**self.base, "history_truncated": None}
        assert "not recorded" in describe_coverage(legacy)
        assert history_complete(legacy) is None
