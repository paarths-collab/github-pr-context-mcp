"""
Tests for entrypoints.webhook_server — signature verification, routing, and security properties.

Documents intentional design decisions:
- Signature bypass when WEBHOOK_SECRET is empty (explicit footgun, documented).
- Ping event returns 200 + "Pong!" without triggering indexing.
- Only merged PRs trigger background indexing, not plain closes.
- Non-pull_request events return 200 OK without side effects.
"""
import json
import hmac
import hashlib
import threading
from io import BytesIO
from unittest.mock import patch, MagicMock, call

import pytest

# Import the functions under test directly — avoids spinning up an HTTPServer
from entrypoints.webhook_server import verify_signature, WebhookHandler, async_index_single_pr


# ── verify_signature ───────────────────────────────────────────────────────────

class TestVerifySignature:

    def _make_sig(self, payload: bytes, secret: str) -> str:
        mac = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256)
        return "sha256=" + mac.hexdigest()

    def test_valid_signature_passes(self):
        payload = b'{"action": "closed"}'
        secret = "mysecret"
        sig = self._make_sig(payload, secret)
        assert verify_signature(payload, secret, sig) is True

    def test_invalid_signature_rejected(self):
        payload = b'{"action": "closed"}'
        assert verify_signature(payload, "mysecret", "sha256=badhash") is False

    def test_missing_signature_header_rejected(self):
        assert verify_signature(b"payload", "mysecret", None) is False

    def test_empty_signature_header_rejected(self):
        assert verify_signature(b"payload", "mysecret", "") is False

    def test_tampered_payload_rejected(self):
        """Changing the payload after signing must invalidate the signature."""
        original = b'{"action": "closed"}'
        tampered = b'{"action": "opened"}'
        sig = self._make_sig(original, "mysecret")
        assert verify_signature(tampered, "mysecret", sig) is False

    def test_empty_secret_bypasses_verification(self):
        """
        KNOWN SECURITY FOOTGUN (documented, intentional):
        When GITHUB_WEBHOOK_SECRET is not configured, ALL requests are accepted.
        This is a conscious dev-mode trade-off. In production, always set the secret.
        If this test ever fails, check if bypass was removed — update docs accordingly.
        """
        # Even with a completely wrong signature, empty secret = pass
        assert verify_signature(b"any payload", "", "sha256=wronghash") is True
        # And with no signature header at all
        assert verify_signature(b"any payload", "", None) is True


# ── WebhookHandler routing ─────────────────────────────────────────────────────

def _make_handler(event_type: str, payload: dict, secret: str = "") -> WebhookHandler:
    """
    Construct a WebhookHandler with mocked socket/request infrastructure.
    Returns the handler with responses captured in handler.wfile.
    """
    body = json.dumps(payload).encode()
    sig = ""
    if secret:
        mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
        sig = "sha256=" + mac.hexdigest()

    headers = {
        "Content-Length": str(len(body)),
        "X-GitHub-Event": event_type,
        "X-Hub-Signature-256": sig,
    }

    request = MagicMock()
    request.makefile = MagicMock(return_value=BytesIO(body))

    handler = WebhookHandler.__new__(WebhookHandler)
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.headers = headers
    handler.server = MagicMock()
    handler.request = request

    # Capture response codes
    handler._response_codes = []

    original_send = WebhookHandler.send_response
    def capturing_send(self, code):
        self._response_codes.append(code)
    handler.send_response = lambda code: capturing_send(handler, code)
    handler.end_headers = MagicMock()

    return handler


class TestWebhookHandlerPing:
    def test_ping_event_returns_200(self):
        handler = _make_handler("ping", {"zen": "Speak your mind even if your voice shakes."})
        handler.do_POST()
        assert 200 in handler._response_codes

    def test_ping_event_returns_pong_body(self):
        handler = _make_handler("ping", {"zen": "test"})
        handler.do_POST()
        handler.wfile.seek(0)
        assert b"Pong!" in handler.wfile.read()

    def test_ping_does_not_trigger_indexing(self):
        with patch("entrypoints.webhook_server.threading.Thread") as mock_thread:
            handler = _make_handler("ping", {"zen": "test"})
            handler.do_POST()
            mock_thread.assert_not_called()


class TestWebhookHandlerPullRequest:
    def _merged_pr_payload(self, pr_number=42, repo="owner/repo") -> dict:
        return {
            "action": "closed",
            "pull_request": {"number": pr_number, "merged": True},
            "repository": {"full_name": repo},
        }

    def test_merged_pr_triggers_background_thread(self):
        with patch("entrypoints.webhook_server.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler = _make_handler("pull_request", self._merged_pr_payload())
            handler.do_POST()
            mock_thread.assert_called_once()
            # Verify it targets the indexing function
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs.get("target") == async_index_single_pr or \
                   call_kwargs[1].get("target") == async_index_single_pr or \
                   async_index_single_pr.__name__ in str(call_kwargs)

    def test_merged_pr_returns_200(self):
        with patch("entrypoints.webhook_server.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler = _make_handler("pull_request", self._merged_pr_payload())
            handler.do_POST()
        assert 200 in handler._response_codes

    def test_closed_but_not_merged_pr_does_not_trigger_indexing(self):
        payload = {
            "action": "closed",
            "pull_request": {"number": 10, "merged": False},
            "repository": {"full_name": "owner/repo"},
        }
        with patch("entrypoints.webhook_server.threading.Thread") as mock_thread:
            handler = _make_handler("pull_request", payload)
            handler.do_POST()
            mock_thread.assert_not_called()

    def test_opened_pr_does_not_trigger_indexing(self):
        payload = {
            "action": "opened",
            "pull_request": {"number": 11, "merged": False},
            "repository": {"full_name": "owner/repo"},
        }
        with patch("entrypoints.webhook_server.threading.Thread") as mock_thread:
            handler = _make_handler("pull_request", payload)
            handler.do_POST()
            mock_thread.assert_not_called()

    def test_invalid_json_does_not_crash(self):
        """Malformed JSON body should be silently swallowed and return 200."""
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.rfile = BytesIO(b"not valid json{{{")
        handler.wfile = BytesIO()
        handler.headers = {
            "Content-Length": "17",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "",
        }
        handler._response_codes = []
        handler.send_response = lambda code: handler._response_codes.append(code)
        handler.end_headers = MagicMock()
        handler.do_POST()
        assert 200 in handler._response_codes


class TestWebhookHandlerSignatureEnforcement:
    def test_wrong_signature_returns_403(self):
        body = json.dumps({"action": "closed"}).encode()
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.headers = {
            "Content-Length": str(len(body)),
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=badhash",
        }
        handler._response_codes = []
        handler.send_response = lambda code: handler._response_codes.append(code)
        handler.end_headers = MagicMock()

        with patch("entrypoints.webhook_server.WEBHOOK_SECRET", "real-secret"):
            handler.do_POST()

        assert 403 in handler._response_codes

    def test_wrong_signature_does_not_trigger_indexing(self):
        body = json.dumps({
            "action": "closed",
            "pull_request": {"number": 1, "merged": True},
            "repository": {"full_name": "owner/repo"},
        }).encode()
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.headers = {
            "Content-Length": str(len(body)),
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=badhash",
        }
        handler._response_codes = []
        handler.send_response = lambda code: handler._response_codes.append(code)
        handler.end_headers = MagicMock()

        with patch("entrypoints.webhook_server.WEBHOOK_SECRET", "real-secret"):
            with patch("entrypoints.webhook_server.threading.Thread") as mock_thread:
                handler.do_POST()
                mock_thread.assert_not_called()


# ── async_index_single_pr — event loop safety ─────────────────────────────────

class TestAsyncIndexSinglePrEventLoopSafety:
    def test_creates_fresh_event_loop_not_asyncio_run(self):
        """
        Verify the function uses asyncio.new_event_loop() explicitly,
        not asyncio.run() which can fail if called from an async context.
        This test would raise RuntimeError with the old asyncio.run() implementation
        when called from within a running event loop (e.g. in an anyio test).
        """
        import asyncio
        import inspect
        import entrypoints.webhook_server as ws

        src = inspect.getsource(ws.async_index_single_pr)
        assert "asyncio.new_event_loop()" in src, (
            "async_index_single_pr must use asyncio.new_event_loop() not asyncio.run(). "
            "asyncio.run() raises RuntimeError when called from a thread with a running loop."
        )
        assert "asyncio.run(" not in src, (
            "asyncio.run() found in async_index_single_pr — this is the unsafe pattern."
        )

    def test_thread_target_is_correct_function(self):
        """The Thread spawned by do_POST must target async_index_single_pr, not a lambda."""
        payload = {
            "action": "closed",
            "pull_request": {"number": 99, "merged": True},
            "repository": {"full_name": "myorg/myrepo"},
        }
        captured_threads = []

        def fake_thread(**kwargs):
            captured_threads.append(kwargs)
            t = MagicMock()
            t.start = MagicMock()
            return t

        with patch("entrypoints.webhook_server.threading.Thread", side_effect=fake_thread):
            handler = _make_handler("pull_request", payload)
            handler.do_POST()

        assert len(captured_threads) == 1
        assert captured_threads[0]["target"] == async_index_single_pr
        assert captured_threads[0]["args"] == ("myorg/myrepo", 99)
        assert captured_threads[0].get("daemon") is True
