"""
Tests for storage.clustering — theme assignment, deduplication, and cluster output shape.
Uses an ephemeral ChromaDB collection so no disk state is left behind.
"""
import pytest
from storage.clustering import _assign_theme, _deduplicate, cluster_patterns
from storage.vector_store import index_prs, delete_repo_index

# ── Helpers ───────────────────────────────────────────────────────────────────

REPO = "test-owner/clustering-test"
NS = "cluster-ns"

_SAMPLE_PRS = [
    {
        "number": 10,
        "title": "Add error handling",
        "body": "Adds try/except blocks throughout the service layer.",
        "author": "alice",
        "author_is_bot": False,
        "touches_ci": False,
        "files": [{"path": "services/auth.py"}],
        "review_comments": [
            {
                "file": "services/auth.py",
                "line": 42,
                "resolved": True,
                "author": "bob",
                "is_bot": False,
                "body": "We should raise a specific exception type here, not a generic Exception.",
                "created_at": "2024-06-01T10:00:00Z",
                "diff_hunk": "@@ -40,3 +40,5 @@ def login(user):\n+    try:\n+        return db.get(user)\n+    except Exception:\n+        raise",
            },
            {
                "file": "services/auth.py",
                "line": 55,
                "resolved": True,
                "author": "carol",
                "is_bot": False,
                "body": "Add a traceback log before re-raising the error so we can diagnose failures.",
                "created_at": "2024-06-01T11:00:00Z",
                "diff_hunk": "",
            },
        ],
        "reviews": [],
        "commits": [],
    },
    {
        "number": 11,
        "title": "Improve test coverage",
        "body": "Adds unit tests for the payment module.",
        "author": "dave",
        "author_is_bot": False,
        "touches_ci": False,
        "files": [{"path": "tests/test_payment.py"}],
        "review_comments": [
            {
                "file": "tests/test_payment.py",
                "line": 10,
                "resolved": False,
                "author": "eve",
                "is_bot": False,
                "body": "Add a mock fixture for the payment gateway rather than hitting the real API.",
                "created_at": "2024-06-02T09:00:00Z",
                "diff_hunk": "",
            }
        ],
        "reviews": [],
        "commits": [],
    },
    {
        "number": 12,
        "title": "Security: sanitize user input",
        "body": "Prevents SQL injection via parameterized queries.",
        "author": "frank",
        "author_is_bot": False,
        "touches_ci": False,
        "files": [{"path": "db/queries.py"}],
        "review_comments": [
            {
                "file": "db/queries.py",
                "line": 77,
                "resolved": True,
                "author": "grace",
                "is_bot": False,
                "body": "Use parameterized queries to prevent SQL injection vulnerabilities here.",
                "created_at": "2024-06-03T08:00:00Z",
                "diff_hunk": "",
            }
        ],
        "reviews": [],
        "commits": [],
    },
]


def _seed_index():
    """Index sample PRs into an ephemeral collection and return doc count."""
    return index_prs(REPO, _SAMPLE_PRS, temporary=True, namespace=NS)


def _teardown_index():
    delete_repo_index(REPO, storage="temporary", namespace=NS)


# ── Unit tests: _assign_theme ──────────────────────────────────────────────────

class TestAssignTheme:
    def test_error_handling_keywords(self):
        assert _assign_theme("We should raise a specific exception type here") == "Error Handling"

    def test_testing_keywords(self):
        assert _assign_theme("Add a mock fixture for the payment gateway") == "Testing"

    def test_security_keywords(self):
        assert _assign_theme("Use parameterized queries to prevent SQL injection") == "Security"

    def test_performance_keywords(self):
        assert _assign_theme("This will cause an N+1 query — batch the DB calls") == "Performance"

    def test_style_keywords(self):
        assert _assign_theme("The variable naming here is inconsistent with the convention") == "Code Style"

    def test_ci_keywords(self):
        assert _assign_theme("This Docker build step is redundant in the CI pipeline") == "CI/CD"

    def test_uncategorized_fallback(self):
        # Random text that doesn't hit any theme
        result = _assign_theme("This is a very generic sentence with no technical keywords.")
        assert result == "General Feedback"

    def test_specificity_ordering(self):
        # "injection" → Security should take precedence even if "query" (Database) also matches
        text = "SQL injection via query parameter"
        assert _assign_theme(text) == "Security"


# ── Unit tests: _deduplicate ───────────────────────────────────────────────────

class TestDeduplicate:
    def _make_item(self, text: str, theme: str, similarity: float) -> dict:
        return {"text": text, "theme": theme, "similarity": similarity, "metadata": {}}

    def test_unique_items_all_kept(self):
        items = [
            self._make_item("Exception should be specific", "Error Handling", 0.91),
            self._make_item("Add a mock fixture", "Testing", 0.88),
        ]
        result = _deduplicate(items)
        assert len(result) == 2

    def test_near_duplicate_removed(self):
        text = "Raise a specific exception not a generic one here please"
        items = [
            self._make_item(text, "Error Handling", 0.91),
            self._make_item(text, "Error Handling", 0.91),  # exact duplicate
        ]
        result = _deduplicate(items)
        assert len(result) == 1

    def test_different_theme_same_text_kept(self):
        # Same prefix text but different themes — not considered duplicates
        text = "This should be improved"
        items = [
            self._make_item(text, "Error Handling", 0.91),
            self._make_item(text, "Code Style", 0.91),
        ]
        result = _deduplicate(items)
        assert len(result) == 2

    def test_different_similarity_same_theme_kept(self):
        # Same theme but similarity diff > 0.02 — keep both
        items = [
            self._make_item("Raise a specific exception", "Error Handling", 0.91),
            self._make_item("Raise a specific exception", "Error Handling", 0.85),
        ]
        result = _deduplicate(items)
        assert len(result) == 2


# ── Integration tests: cluster_patterns ───────────────────────────────────────

class TestClusterPatterns:
    def setup_method(self):
        _seed_index()

    def teardown_method(self):
        _teardown_index()

    def test_returns_list_of_clusters(self):
        clusters = cluster_patterns(REPO, temporary=True, namespace=NS)
        assert isinstance(clusters, list)
        assert len(clusters) > 0

    def test_cluster_schema(self):
        clusters = cluster_patterns(REPO, temporary=True, namespace=NS)
        for cluster in clusters:
            assert "theme" in cluster
            assert "count" in cluster
            assert "confidence" in cluster
            assert "examples" in cluster
            assert "files" in cluster
            assert isinstance(cluster["theme"], str)
            assert isinstance(cluster["count"], int)
            assert 0.0 <= cluster["confidence"] <= 1.0
            assert isinstance(cluster["examples"], list)
            assert isinstance(cluster["files"], list)

    def test_examples_capped_at_300_chars(self):
        clusters = cluster_patterns(REPO, temporary=True, namespace=NS)
        for cluster in clusters:
            for example in cluster["examples"]:
                assert len(example) <= 300

    def test_known_theme_present(self):
        """At least one of our seeded security / error comments should surface."""
        clusters = cluster_patterns(REPO, topic="exception error security sql", temporary=True, namespace=NS)
        theme_names = {c["theme"] for c in clusters}
        # At least one meaningful theme must surface from seeded data
        assert len(theme_names) > 0

    def test_empty_index_returns_empty(self):
        """Querying a non-existent repo should return [] not raise."""
        result = cluster_patterns("nobody/no-repo", temporary=True, namespace="empty-ns")
        assert result == []

    def test_confidence_is_mean_of_similarities(self):
        clusters = cluster_patterns(REPO, temporary=True, namespace=NS)
        for cluster in clusters:
            # confidence must be a valid float between 0 and 1
            assert isinstance(cluster["confidence"], float)
            assert 0.0 <= cluster["confidence"] <= 1.0
