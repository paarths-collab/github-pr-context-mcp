"""
Tests for storage.document_builder — is_high_quality_comment and build_documents.

Documents intentional design decisions as tests:
- The 'nit:' threshold (25 chars): short nit comments are noise, long ones are signal.
- Review summaries always pass is_bot=False (known gap, documented here).
- IDs uniqueness guarantee across a multi-PR build.
- Graceful handling of missing diff_hunk.
"""
import pytest
from storage.document_builder import is_high_quality_comment, build_documents


# ── is_high_quality_comment ────────────────────────────────────────────────────

class TestIsHighQualityComment:

    # --- Bot filtering ---

    def test_bot_comment_rejected(self):
        assert is_high_quality_comment("This is a well-reasoned review comment.", is_bot=True) is False

    def test_human_substantive_passes(self):
        assert is_high_quality_comment("We should raise a specific exception here.", is_bot=False) is True

    # --- Length threshold ---

    def test_too_short_rejected(self):
        assert is_high_quality_comment("Good job!", is_bot=False) is False  # 9 chars

    def test_exactly_15_chars_passes(self):
        # 15 chars, not a low-signal phrase
        assert is_high_quality_comment("Great addition!", is_bot=False) is True

    def test_14_chars_rejected(self):
        assert is_high_quality_comment("Nice addition!", is_bot=False) is False  # 14 chars

    # --- Exact low-signal phrases ---

    def test_lgtm_rejected(self):
        assert is_high_quality_comment("lgtm", is_bot=False) is False

    def test_lgtm_uppercase_rejected(self):
        assert is_high_quality_comment("LGTM", is_bot=False) is False

    def test_looks_good_rejected(self):
        assert is_high_quality_comment("looks good", is_bot=False) is False

    def test_looks_good_to_me_rejected(self):
        assert is_high_quality_comment("Looks good to me", is_bot=False) is False

    def test_plus_one_rejected(self):
        assert is_high_quality_comment("+1", is_bot=False) is False

    def test_agreed_rejected(self):
        assert is_high_quality_comment("agreed", is_bot=False) is False

    def test_addressed_rejected(self):
        assert is_high_quality_comment("addressed", is_bot=False) is False

    # --- nit: prefix rule ---

    def test_short_nit_rejected(self):
        # "nit: ok" = 7 chars → well under 25 threshold → filtered
        assert is_high_quality_comment("nit: ok", is_bot=False) is False

    def test_nit_just_under_threshold_rejected(self):
        # 24 chars — just under the 25-char minimum
        body = "nit: rename to snake_case"  # len = 25 — right at boundary
        # At exactly 25 it passes; test 24 chars to confirm rejection
        body_24 = "nit: rename to snake_cas"  # len = 24
        assert len(body_24) == 24
        assert is_high_quality_comment(body_24, is_bot=False) is False

    def test_nit_at_threshold_passes(self):
        # Exactly 25 chars should pass (>= 25)
        body = "nit: rename to snake_case"
        assert len(body) == 25
        assert is_high_quality_comment(body, is_bot=False) is True

    def test_substantive_nit_passes(self):
        # "nit: rename this variable to follow PEP8 conventions" is clearly signal
        assert is_high_quality_comment(
            "nit: rename this variable to follow PEP8 conventions",
            is_bot=False,
        ) is True

    def test_nit_prefix_case_insensitive(self):
        # "NIT: ok" should also be filtered
        assert is_high_quality_comment("NIT: short", is_bot=False) is False

    # --- Normal substantive comments always pass ---

    def test_error_handling_comment_passes(self):
        assert is_high_quality_comment(
            "We should raise a specific exception type, not a generic Exception.",
            is_bot=False,
        ) is True

    def test_security_comment_passes(self):
        assert is_high_quality_comment(
            "Use parameterized queries to prevent SQL injection here.",
            is_bot=False,
        ) is True


# ── build_documents ────────────────────────────────────────────────────────────

def _minimal_pr(number=1, **overrides) -> dict:
    """Return a minimal valid PR dict, with easy override of any field."""
    base = {
        "number": number,
        "title": "Test PR",
        "body": "This PR adds a feature to the authentication module.",
        "author": "alice",
        "author_is_bot": False,
        "touches_ci": False,
        "files": [{"path": "src/auth.py"}],
        "review_comments": [],
        "reviews": [],
        "commits": [],
    }
    base.update(overrides)
    return base


class TestBuildDocuments:

    # --- Basic output contract ---

    def test_returns_three_lists(self):
        docs, metadatas, ids = build_documents([_minimal_pr()])
        assert isinstance(docs, list)
        assert isinstance(metadatas, list)
        assert isinstance(ids, list)

    def test_lists_same_length(self):
        pr = _minimal_pr()
        docs, metadatas, ids = build_documents([pr])
        assert len(docs) == len(metadatas) == len(ids)

    def test_empty_input_returns_empty_output(self):
        docs, metadatas, ids = build_documents([])
        assert docs == [] and metadatas == [] and ids == []

    # --- PR description ---

    def test_pr_description_included_when_body_present(self):
        docs, _, ids = build_documents([_minimal_pr(number=5)])
        assert any("pr-5-desc" in i for i in ids)

    def test_pr_description_excluded_when_body_empty(self):
        pr = _minimal_pr(body="")
        docs, _, ids = build_documents([pr])
        assert not any("desc" in i for i in ids)

    def test_pr_description_excluded_when_body_whitespace(self):
        pr = _minimal_pr(body="   \n  ")
        _, _, ids = build_documents([pr])
        assert not any("desc" in i for i in ids)

    # --- Review comments ---

    def test_high_quality_comment_included(self):
        pr = _minimal_pr(review_comments=[{
            "file": "src/auth.py",
            "line": 10,
            "resolved": True,
            "author": "bob",
            "is_bot": False,
            "body": "We should raise a specific exception type here instead of bare Exception.",
            "created_at": "2024-01-01T00:00:00Z",
            "diff_hunk": "@@ -9,3 +9,5 @@",
        }])
        _, _, ids = build_documents([pr])
        assert any("comment" in i for i in ids)

    def test_low_quality_comment_excluded(self):
        pr = _minimal_pr(review_comments=[{
            "file": "src/auth.py",
            "line": 10,
            "resolved": True,
            "author": "bob",
            "is_bot": False,
            "body": "lgtm",
            "created_at": "2024-01-01T00:00:00Z",
            "diff_hunk": "",
        }])
        _, _, ids = build_documents([pr])
        assert not any("comment" in i for i in ids)

    def test_bot_comment_excluded(self):
        pr = _minimal_pr(review_comments=[{
            "file": "src/auth.py",
            "line": 1,
            "resolved": False,
            "author": "renovate[bot]",
            "is_bot": True,
            "body": "Dependency update available for cryptography>=42.0.0 for security fixes.",
            "created_at": "2024-01-01T00:00:00Z",
            "diff_hunk": "",
        }])
        _, _, ids = build_documents([pr])
        assert not any("comment" in i for i in ids)

    def test_missing_diff_hunk_gracefully_omitted(self):
        """A review comment with no diff_hunk should still produce a document — just without code context."""
        pr = _minimal_pr(review_comments=[{
            "file": "src/auth.py",
            "line": 10,
            "resolved": False,
            "author": "carol",
            "is_bot": False,
            "body": "This function is doing too many things — split it into smaller helpers.",
            "created_at": "2024-01-01T00:00:00Z",
            # diff_hunk key intentionally omitted
        }])
        docs, _, ids = build_documents([pr])
        assert any("comment" in i for i in ids)
        comment_doc = next(d for d, i in zip(docs, ids) if "comment" in i)
        assert "Code Context" not in comment_doc  # no hunk injected

    def test_empty_diff_hunk_gracefully_omitted(self):
        """An explicit empty string for diff_hunk should also produce no code context block."""
        pr = _minimal_pr(review_comments=[{
            "file": "src/auth.py",
            "line": 10,
            "resolved": False,
            "author": "dave",
            "is_bot": False,
            "body": "Consider using a context manager here for automatic resource cleanup.",
            "created_at": "2024-01-01T00:00:00Z",
            "diff_hunk": "",
        }])
        docs, _, ids = build_documents([pr])
        comment_doc = next((d for d, i in zip(docs, ids) if "comment" in i), None)
        assert comment_doc is not None
        assert "Code Context" not in comment_doc

    def test_diff_hunk_included_in_document_when_present(self):
        hunk = "@@ -10,3 +10,5 @@ def login(user):\n+    try:\n+        return db.get(user)"
        pr = _minimal_pr(review_comments=[{
            "file": "src/auth.py",
            "line": 10,
            "resolved": False,
            "author": "eve",
            "is_bot": False,
            "body": "Wrap this in a try/except and raise a domain-specific exception.",
            "created_at": "2024-01-01T00:00:00Z",
            "diff_hunk": hunk,
        }])
        docs, _, ids = build_documents([pr])
        comment_doc = next((d for d, i in zip(docs, ids) if "comment" in i), None)
        assert comment_doc is not None
        assert "Code Context" in comment_doc
        assert hunk in comment_doc

    # --- Review summaries (known bot gap) ---

    def test_review_summary_included_when_substantive(self):
        pr = _minimal_pr(reviews=[{
            "author": "frank",
            "state": "CHANGES_REQUESTED",
            "body": "The error handling is incomplete — wrap all external calls in try/except.",
            "submitted_at": "2024-01-01T00:00:00Z",
        }])
        _, _, ids = build_documents([pr])
        assert any("review" in i for i in ids)

    def test_review_summary_excluded_when_low_signal(self):
        pr = _minimal_pr(reviews=[{
            "author": "grace",
            "state": "APPROVED",
            "body": "lgtm",
            "submitted_at": "2024-01-01T00:00:00Z",
        }])
        _, _, ids = build_documents([pr])
        assert not any("review" in i for i in ids)

    def test_review_summary_bot_is_not_detected(self):
        """
        KNOWN DESIGN GAP (documented, not a bug to fix now):
        Review summaries currently pass is_bot=False unconditionally because
        the 'reviews' list doesn't carry an is_bot flag. A bot like 'dependabot'
        writing an APPROVED review will be indexed if it passes the text quality filter.
        This test documents that behaviour so it's intentional, not accidental.
        """
        pr = _minimal_pr(reviews=[{
            "author": "dependabot[bot]",
            "state": "APPROVED",
            # This body is substantive enough to pass the quality filter
            "body": "Bumps cryptography from 41.0.0 to 42.0.8. This resolves CVE-2024-0727.",
            "submitted_at": "2024-01-01T00:00:00Z",
        }])
        _, _, ids = build_documents([pr])
        # Bot review IS included — this is the gap. If this assertion ever fails,
        # it means bot detection was added to reviews and the test should be updated.
        assert any("review" in i for i in ids), (
            "If this fails: bot detection was added to review summaries. "
            "Remove this test and verify via test_bot_review_excluded instead."
        )

    # --- IDs uniqueness guarantee ---

    def test_ids_are_unique_within_single_pr(self):
        pr = _minimal_pr(
            number=42,
            review_comments=[
                {
                    "file": "a.py", "line": 1, "resolved": False,
                    "author": "x", "is_bot": False,
                    "body": "This approach will cause an N+1 query — use select_related.",
                    "created_at": "2024-01-01T00:00:00Z", "diff_hunk": "",
                },
                {
                    "file": "b.py", "line": 5, "resolved": True,
                    "author": "y", "is_bot": False,
                    "body": "Missing type annotation on the return value of this function.",
                    "created_at": "2024-01-01T00:00:00Z", "diff_hunk": "",
                },
            ],
            commits=[{"message": "fix: improve query efficiency"}],
            reviews=[{
                "author": "z", "state": "APPROVED",
                "body": "Overall good — just address the N+1 and type annotation issues.",
                "submitted_at": "2024-01-01T00:00:00Z",
            }],
        )
        _, _, ids = build_documents([pr])
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    def test_ids_are_unique_across_multiple_prs(self):
        prs = [_minimal_pr(number=n) for n in range(1, 6)]
        _, _, ids = build_documents(prs)
        assert len(ids) == len(set(ids)), f"Duplicate IDs across PRs: {ids}"

    def test_id_format_is_stable(self):
        """IDs must follow the pr-N-TYPE-I pattern so ChromaDB upserts are idempotent."""
        pr = _minimal_pr(
            number=7,
            review_comments=[{
                "file": "x.py", "line": 1, "resolved": False,
                "author": "u", "is_bot": False,
                "body": "Consider extracting this logic into a dedicated helper function.",
                "created_at": "2024-01-01T00:00:00Z", "diff_hunk": "",
            }],
        )
        _, _, ids = build_documents([pr])
        assert "pr-7-desc" in ids
        assert "pr-7-comment-0" in ids
