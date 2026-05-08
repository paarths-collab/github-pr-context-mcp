import pytest
import os
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
        
    finally:
        if os.path.exists(path):
            os.remove(path)
