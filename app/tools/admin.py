import json
import asyncio
from mcp.server.fastmcp import Context
from starlette.responses import JSONResponse
from app.state import (
    AUTH_REQUIRED,
    identity_store,
    usage_store,
    current_user_email,
    validate_admin_token
)

def register_admin_tools(mcp):
    @mcp.tool(name="update_settings")
    async def update_settings(
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Update legacy hosted model settings; GitHub tokens are never accepted here."""
        if not AUTH_REQUIRED or identity_store is None:
            return "Warning: This server is in Local Mode. To update your settings, please update your environment variables or IDE configuration (e.g. claude_desktop_config.json)."

        if ctx is None:
            raise ValueError("Context is required")

        email = current_user_email()
        if not email:
            return "Error: Could not identify your user identity. Are you logged in via Bearer token?"

        new_settings = {}
        if llm_provider: new_settings["llm_provider"] = llm_provider
        if llm_model: new_settings["llm_model"] = llm_model
        if llm_api_key: new_settings["llm_api_key"] = llm_api_key

        if not new_settings:
            return "No settings provided to update."

        try:
            await asyncio.to_thread(identity_store.update_user_settings, email, new_settings)
            return f"Successfully updated your settings: {', '.join(new_settings.keys())}."
        except Exception as e:
            return f"Failed to update settings: {str(e)}"

    @mcp.tool(name="get_usage_stats")
    async def get_usage_stats(days: int = 30, admin_token: str | None = None) -> str:
        """Return anonymous usage metrics (tool calls, unique users, top tools)."""
        if usage_store is None:
            return json.dumps({"enabled": False, "reason": "USAGE_TRACKING_ENABLED=false"}, indent=2)

        if not validate_admin_token(admin_token):
            return "Unauthorized: provide a valid admin_token."

        days = max(1, min(days, 365))
        stats = await asyncio.to_thread(usage_store.summary, last_days=days)
        return json.dumps(stats, indent=2)
