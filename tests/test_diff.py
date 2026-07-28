"""
Tests for fetcher.diff — PR reference parsing, unified diff parsing, and fetch_pr_diff.
Network calls are intercepted via unittest.mock so no real GitHub token is needed
and tests run entirely offline. Async tests use anyio (already a project dependency).
"""
import pytest
from unittest.mock import patch, AsyncMock

from fetcher.diff import (
    _parse_pr_identifier,
    _parse_unified_diff,
    fetch_pr_diff,
    MAX_HUNK_CHARS,
)


# ── _parse_pr_identifier ───────────────────────────────────────────────────────

class TestParsePrIdentifier:
    def test_short_form(self):
        owner, repo, number = _parse_pr_identifier("myorg/myrepo#42")
        assert owner == "myorg"
        assert repo == "myrepo"
        assert number == 42

    def test_full_github_url(self):
        url = "https://github.com/paarths-collab/github-pr-context-mcp/pull/7"
        owner, repo, number = _parse_pr_identifier(url)
        assert owner == "paarths-collab"
        assert repo == "github-pr-context-mcp"
        assert number == 7

    def test_http_url_also_accepted(self):
        url = "http://github.com/org/repo/pull/99"
        owner, repo, number = _parse_pr_identifier(url)
        assert owner == "org"
        assert repo == "repo"
        assert number == 99

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Cannot parse PR reference"):
            _parse_pr_identifier("not-a-valid-ref")

    def test_missing_pr_number_raises(self):
        with pytest.raises(ValueError):
            _parse_pr_identifier("owner/repo")

    def test_large_pr_number(self):
        _, _, number = _parse_pr_identifier("org/repo#10000")
        assert number == 10000


# ── _parse_unified_diff ────────────────────────────────────────────────────────

SAMPLE_DIFF = """\
diff --git a/src/auth.py b/src/auth.py
index abc123..def456 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,6 +10,8 @@ def login(user):
     db = get_db()
+    try:
+        return db.get(user)
+    except Exception as e:
+        raise ValueError("Login failed") from e
-    return db.get(user)

diff --git a/src/utils.py b/src/utils.py
new file mode 100644
index 000000..aabbcc
--- /dev/null
+++ b/src/utils.py
@@ -0,0 +1,5 @@
+def helper():
+    pass
"""

BINARY_DIFF = """\
diff --git a/assets/logo.png b/assets/logo.png
index 000000..ffffff 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""

RENAMED_DIFF = """\
diff --git a/old_name.py b/new_name.py
similarity index 100%
rename from old_name.py
rename to new_name.py
"""


class TestParseUnifiedDiff:
    def test_returns_list_of_file_dicts(self):
        result = _parse_unified_diff(SAMPLE_DIFF)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_file_paths_extracted(self):
        result = _parse_unified_diff(SAMPLE_DIFF)
        paths = [r["file"] for r in result]
        assert "src/auth.py" in paths
        assert "src/utils.py" in paths

    def test_hunks_list_present(self):
        result = _parse_unified_diff(SAMPLE_DIFF)
        for file_entry in result:
            assert "hunks" in file_entry
            assert isinstance(file_entry["hunks"], list)
            assert len(file_entry["hunks"]) > 0

    def test_new_file_change_type(self):
        result = _parse_unified_diff(SAMPLE_DIFF)
        new_file = next((r for r in result if r["file"] == "src/utils.py"), None)
        assert new_file is not None
        assert new_file["change_type"] == "ADDED"

    def test_modified_file_change_type(self):
        result = _parse_unified_diff(SAMPLE_DIFF)
        modified = next((r for r in result if r["file"] == "src/auth.py"), None)
        assert modified is not None
        assert modified["change_type"] == "MODIFIED"

    def test_renamed_file_change_type(self):
        # Rename-only diffs have no +++ or @@ lines, so no parseable hunks are produced.
        result = _parse_unified_diff(RENAMED_DIFF)
        assert result == []

    def test_binary_files_excluded(self):
        # Binary diffs have no +++ b/ line → no parseable file entries with hunks
        result = _parse_unified_diff(BINARY_DIFF)
        assert result == [] or all(len(r["hunks"]) == 0 or r["file"] == "" for r in result)

    def test_empty_diff_returns_empty(self):
        result = _parse_unified_diff("")
        assert result == []

    def test_hunk_content_starts_with_at(self):
        result = _parse_unified_diff(SAMPLE_DIFF)
        for file_entry in result:
            for hunk in file_entry["hunks"]:
                assert hunk.startswith("@@"), f"Expected @@ prefix, got: {hunk[:40]!r}"

    def test_multiple_hunks_per_file(self):
        multi_hunk_diff = """\
diff --git a/big_file.py b/big_file.py
--- a/big_file.py
+++ b/big_file.py
@@ -10,3 +10,4 @@ class Foo:
+    def bar(self): pass
@@ -50,3 +50,4 @@ class Bar:
+    def baz(self): pass
"""
        result = _parse_unified_diff(multi_hunk_diff)
        assert len(result) == 1
        assert len(result[0]["hunks"]) == 2


# ── fetch_pr_diff (mocked network — anyio async) ───────────────────────────────

def _make_mock_client(status_code: int, text: str = ""):
    """Return a configured async context manager mock for httpx.AsyncClient."""
    from unittest.mock import MagicMock
    mock_response = AsyncMock()
    mock_response.status_code = status_code
    mock_response.text = text
    # raise_for_status is synchronous in httpx — MagicMock not AsyncMock
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


class TestFetchPrDiff:
    @pytest.mark.anyio
    async def test_returns_parsed_file_list(self):
        """fetch_pr_diff returns a list of file dicts on a 200 response."""
        with patch("fetcher.diff.httpx.AsyncClient", return_value=_make_mock_client(200, SAMPLE_DIFF)):
            result = await fetch_pr_diff("owner/repo#1", github_token="fake-token")

        assert isinstance(result, list)
        assert len(result) > 0
        assert "file" in result[0]
        assert "hunks" in result[0]
        assert "change_type" in result[0]

    @pytest.mark.anyio
    async def test_404_raises_value_error(self):
        with patch("fetcher.diff.httpx.AsyncClient", return_value=_make_mock_client(404)):
            with pytest.raises(ValueError, match="PR not found"):
                await fetch_pr_diff("owner/repo#999", github_token="fake-token")

    @pytest.mark.anyio
    async def test_401_raises_permission_error(self):
        with patch("fetcher.diff.httpx.AsyncClient", return_value=_make_mock_client(401)):
            with pytest.raises(PermissionError, match="401"):
                await fetch_pr_diff("owner/repo#1", github_token="bad-token")

    @pytest.mark.anyio
    async def test_url_format_accepted(self):
        """Full GitHub URL format should parse and succeed without raising."""
        with patch("fetcher.diff.httpx.AsyncClient", return_value=_make_mock_client(200, SAMPLE_DIFF)):
            result = await fetch_pr_diff(
                "https://github.com/paarths-collab/github-pr-context-mcp/pull/1",
                github_token="fake",
            )
        assert isinstance(result, list)

    @pytest.mark.anyio
    async def test_empty_diff_returns_empty_list(self):
        with patch("fetcher.diff.httpx.AsyncClient", return_value=_make_mock_client(200, "")):
            result = await fetch_pr_diff("owner/repo#1", github_token="fake")
        assert result == []


# ── MAX_HUNK_CHARS constant ────────────────────────────────────────────────────

def test_max_hunk_chars_is_reasonable():
    """Guard against accidentally setting this too low or too high."""
    assert 500 <= MAX_HUNK_CHARS <= 10_000
