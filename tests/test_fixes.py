import os
import pytest
from unittest.mock import patch, MagicMock

# Import the actual methods we modified
from entrypoints.local.server import _machine_fingerprint, _detect_mode
from app.mcp_app import _resolve_namespace
import storage.vector_store as vs


### Test Bug 1: Windows `os.getlogin()` Fallback
@patch("os.getlogin")
def test_machine_fingerprint_windows_crash_fallback(mock_getlogin):
    """
    Simulates Bug 1: IDEs launching the MCP server on Windows often have no TTY,
    causing os.getlogin() to throw an OSError.
    We assert that the fingerprint logic catches this and uses fallbacks.
    """
    # Simulate the crash
    mock_getlogin.side_effect = OSError("Inappropriate ioctl for device")
    
    # Fingerprint should successfully compute without bubbling up the crash
    fingerprint = _machine_fingerprint()
    assert fingerprint is not None
    assert len(fingerprint) == 32
    assert isinstance(fingerprint, str)


### Test Bug 4: _detect_mode false positive for uvx on Windows
@patch.dict(os.environ, {"PATH": "C:\\uvx\\bin", "UV_PROJECT_ENVIRONMENT": ""}, clear=True)
def test_detect_mode_no_false_positive():
    """
    Simulates Bug 4: PATH contains 'uvx', but we are not inside a managed uvx env.
    Should fallback to 'local'.
    """
    assert _detect_mode() == "local"

@patch.dict(os.environ, {"UV_PROJECT_ENVIRONMENT": "/tmp/uvx/env"}, clear=True)
def test_detect_mode_true_positive():
    """
    If UV_PROJECT_ENVIRONMENT is set, it genuinely is uvx.
    """
    assert _detect_mode() == "uvx"


### Test Bug 7: CHROMA_PERSIST_DIR absolute path default
def test_chroma_persist_dir_default():
    """
    Bug 7: The storage dir defaulted to './chroma_db', wiping out data whenever uvx 
    was run in a transient dir. It should now default to a stable absolute path.
    """
    # Check the actual fallback default encoded in the file, representing what happens if env is missing
    expected_path = os.path.join(os.path.expanduser("~"), ".github-pr-mcp", "chroma_db")
    assert vs._DEFAULT_CHROMA_DIR == expected_path


### Test Bug 2: Auth Exception handling wraps correctly 
@patch("app.mcp_app.AUTH_REQUIRED", True)
@patch("app.mcp_app._current_user_email", return_value=None)
def test_resolve_namespace_unauthorized_exception(mock_email):
    """
    Bug 2: When users aren't authenticated locally, but AUTH_REQUIRED is True 
    (from Render or env vars), it strictly protects namespace access and throws exactly ValueError. 
    This is what FastMCP expects to serialize an `isError=True` Tool Response.
    """
    with pytest.raises(ValueError) as excinfo:
        _resolve_namespace("ceo@gmail.com", {})
    
    assert "Unauthorized: missing identity when AUTH_REQUIRED is true." in str(excinfo.value)


@patch("app.mcp_app.AUTH_REQUIRED", True)
@patch("app.mcp_app._current_user_email", return_value="alice@gmail.com")
def test_resolve_namespace_authorized(mock_email):
    """
    Bug 2 related: Even if a user tries to access 'bob@gmail.com' (IDOR attempt), 
    AUTH_REQUIRED ensures the namespace strictly locks to 'alice@gmail.com'.
    """
    resolved = _resolve_namespace("bob@gmail.com", {})
    assert resolved == "alice@gmail.com"
