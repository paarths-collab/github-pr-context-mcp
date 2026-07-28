"""MCP tools for the local GitHub App Device Flow connection."""

from __future__ import annotations

import json

from auth import GitHubAuthorizationError
from app.state import LOCAL_GITHUB_DEVICE_FLOW_ENABLED, github_auth_service


def _local_only_result() -> str:
    return json.dumps(
        {
            "status": "unsupported",
            "connected": False,
            "message": (
                "GitHub App Device Flow in v0.3 is supported only for the local "
                "stdio MCP. A hosted service needs a separate tenant-aware secret "
                "vault and is not enabled by these tools."
            ),
        },
        indent=2,
    )


def _safe_error(exc: GitHubAuthorizationError) -> str:
    return json.dumps(
        {"status": "error", "connected": False, "message": str(exc)},
        indent=2,
    )


def register_github_auth_tools(mcp) -> None:
    """Register user-consent tools; no tool ever accepts or returns a GitHub token."""

    @mcp.tool(name="get_github_connection_status")
    async def get_github_connection_status() -> str:
        """Return local GitHub connection state without exposing credentials."""
        if not LOCAL_GITHUB_DEVICE_FLOW_ENABLED or github_auth_service is None:
            return _local_only_result()
        try:
            return json.dumps(await github_auth_service.connection_status(), indent=2)
        except GitHubAuthorizationError as exc:
            return _safe_error(exc)

    @mcp.tool(name="begin_github_authorization")
    async def begin_github_authorization() -> str:
        """Start GitHub App Device Flow and return only a browser URL and one-time user code."""
        if not LOCAL_GITHUB_DEVICE_FLOW_ENABLED or github_auth_service is None:
            return _local_only_result()
        try:
            return json.dumps(await github_auth_service.begin_authorization(), indent=2)
        except GitHubAuthorizationError as exc:
            return _safe_error(exc)

    @mcp.tool(name="complete_github_authorization")
    async def complete_github_authorization() -> str:
        """Poll once after browser approval and save the result in the OS credential vault."""
        if not LOCAL_GITHUB_DEVICE_FLOW_ENABLED or github_auth_service is None:
            return _local_only_result()
        try:
            return json.dumps(await github_auth_service.complete_authorization(), indent=2)
        except GitHubAuthorizationError as exc:
            return _safe_error(exc)

    @mcp.tool(name="disconnect_github")
    async def disconnect_github() -> str:
        """Delete the local GitHub credential from the operating-system vault."""
        if not LOCAL_GITHUB_DEVICE_FLOW_ENABLED or github_auth_service is None:
            return _local_only_result()
        try:
            return json.dumps(await github_auth_service.disconnect(), indent=2)
        except GitHubAuthorizationError as exc:
            return _safe_error(exc)
