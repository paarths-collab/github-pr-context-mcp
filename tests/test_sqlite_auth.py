import pytest
import os
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
