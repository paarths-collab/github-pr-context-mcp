"""Public MCP-tool contracts for local GitHub authorization."""

import asyncio
import json

import pytest

import app.state as app_state
import app.tools.github_auth as github_auth
from auth import GitHubAuthorizationRequired


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def register(func):
            self.tools[name] = func
            return func

        return register


class FakeGitHubAuthService:
    async def connection_status(self):
        return {"status": "disconnected", "connected": False}

    async def begin_authorization(self):
        return {
            "status": "authorization_pending",
            "verification_uri": "https://github.com/login/device",
            "user_code": "ABCD-EFGH",
        }

    async def complete_authorization(self):
        return {"status": "connected", "connected": True, "login": "octocat"}

    async def disconnect(self):
        return {"status": "disconnected", "connected": False}


def test_github_auth_tools_are_registered_and_token_free(monkeypatch):
    mcp = FakeMCP()
    monkeypatch.setattr(github_auth, "LOCAL_GITHUB_DEVICE_FLOW_ENABLED", True)
    monkeypatch.setattr(github_auth, "github_auth_service", FakeGitHubAuthService())
    github_auth.register_github_auth_tools(mcp)

    assert set(mcp.tools) == {
        "get_github_connection_status",
        "begin_github_authorization",
        "complete_github_authorization",
        "disconnect_github",
    }

    challenge = json.loads(asyncio.run(mcp.tools["begin_github_authorization"]()))
    completed = json.loads(asyncio.run(mcp.tools["complete_github_authorization"]()))
    assert challenge == {
        "status": "authorization_pending",
        "verification_uri": "https://github.com/login/device",
        "user_code": "ABCD-EFGH",
    }
    assert completed == {"status": "connected", "connected": True, "login": "octocat"}
    assert "token" not in json.dumps(challenge).lower()
    assert "token" not in json.dumps(completed).lower()


def test_github_auth_tools_are_explicitly_local_only(monkeypatch):
    mcp = FakeMCP()
    monkeypatch.setattr(github_auth, "LOCAL_GITHUB_DEVICE_FLOW_ENABLED", False)
    github_auth.register_github_auth_tools(mcp)

    result = json.loads(asyncio.run(mcp.tools["get_github_connection_status"]()))

    assert result["status"] == "unsupported"
    assert result["connected"] is False


def test_hosted_mode_never_resolves_a_local_github_vault_credential(monkeypatch):
    """A hosted process must not share one local Device Flow approval across tenants."""

    class LocalVaultService:
        called = False

        async def get_access_token(self):
            self.called = True
            return "must-not-be-used"

    service = LocalVaultService()
    monkeypatch.setattr(app_state, "LOCAL_GITHUB_DEVICE_FLOW_ENABLED", False)
    monkeypatch.setattr(app_state, "github_auth_service", service)

    with pytest.raises(GitHubAuthorizationRequired, match="outside the local stdio MCP"):
        asyncio.run(app_state.get_github_access_token())

    assert service.called is False
