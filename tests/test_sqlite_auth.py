import pytest
import os
import json
import threading
from auth.gmail_identity import GmailIdentityStore

@pytest.fixture
def auth_store(tmp_path):
    db_path = tmp_path / "test_auth.db"
    store = GmailIdentityStore(str(db_path))
    return store

def test_sqlite_concurrent_inserts(auth_store):
    """
    Simulates high web-worker scale writing via concurrently registering emails.
    If JSON locking fails, it corrupts the file. SQLite should gracefully block or insert them all.
    """
    total_threads = 50
    results = []

    def register_worker(index):
        try:
            auth_store.register_email(f"user{index}@gmail.com")
            results.append(True)
        except Exception as e:
            results.append(False)

    threads = []
    for i in range(total_threads):
        t = threading.Thread(target=register_worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # If it was thread-safe, 50 rows must be inserted without corruption
    assert sum(results) == total_threads

def test_sqlite_get_token(auth_store):
    # Tests that retrieval handles properly formatted models mapped back to dict
    result = auth_store.register_email("ceo@gmail.com", {"llm_provider": "anthropic"})
    settings = auth_store.get_user_settings("ceo@gmail.com")
    assert settings.get("llm_provider") == "anthropic"

    # Verify IDOR identity scopes
    token_verified = auth_store.verify_token(result.token)
    assert token_verified.client_id == "ceo@gmail.com"


def test_identity_store_rejects_plaintext_github_tokens(auth_store):
    """Hosted SQLite settings must never become a fallback credential vault."""
    with pytest.raises(ValueError, match="github_token is not accepted"):
        auth_store.register_email(
            "secure@gmail.com",
            {"github_token": "never-write-a-github-token-here"},
        )


def test_identity_store_purges_a_legacy_github_token_before_returning_settings(auth_store):
    """Rows from older releases must not leak a PAT through the settings API."""
    auth_store.register_email("legacy@gmail.com", {"llm_provider": "openai"})
    legacy = {
        "github_token": "ghp_legacy_token_that_must_never_be_returned",
        "llm_provider": "openai",
    }
    with auth_store._get_conn() as conn:
        conn.execute(
            "UPDATE users SET settings = ? WHERE email = ?",
            (json.dumps(legacy), "legacy@gmail.com"),
        )

    updated = auth_store.update_user_settings(
        "legacy@gmail.com", {"llm_model": "gpt-4.1"}
    )
    persisted = auth_store.get_user_settings("legacy@gmail.com")

    assert "github_token" not in updated
    assert "github_token" not in persisted
    assert "legacy_token_that_must_never_be_returned" not in json.dumps(updated)
    assert "legacy_token_that_must_never_be_returned" not in json.dumps(persisted)
