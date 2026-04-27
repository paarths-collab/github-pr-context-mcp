from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server.auth.middleware.auth_context import get_access_token
from app.state import (
    identity_store,
    usage_store,
    current_user_email,
    current_user_settings,
    validate_admin_token
)

def register_http_routes(mcp):
    @mcp.custom_route("/settings", methods=["PUT"], include_in_schema=False)
    async def update_settings_route(request: Request):
        access_token = get_access_token()
        if access_token is None or identity_store is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(settings, dict):
            return JSONResponse({"error": "settings must be an object"}, status_code=400)

        try:
            updated = identity_store.update_user_settings(access_token.client_id, settings)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return JSONResponse({"email": access_token.client_id, "settings": updated})

    @mcp.custom_route("/whoami", methods=["GET"], include_in_schema=False)
    async def whoami_route(_: Request):
        access_token = get_access_token()
        if access_token is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        user_settings = current_user_settings()
        return JSONResponse({
            "email": access_token.client_id,
            "scopes": access_token.scopes,
            "has_custom_github_token": bool(user_settings.get("github_token")),
            "has_custom_llm": any(user_settings.get(k) for k in ("llm_provider", "llm_model", "llm_api_key", "llm_base_url")),
        })

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request):
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/ping", methods=["POST"], include_in_schema=False)
    async def ping(request: Request):
        """Anonymous telemetry ping from CLI (uvx/pipx)."""
        if usage_store is None:
            return JSONResponse({"enabled": False}, status_code=503)
        try:
            payload = await request.json()
            uid = payload.get("id")
            mode = payload.get("mode", "unknown")
            if not uid:
                return JSONResponse({"error": "invalid_id"}, status_code=400)
            usage_store.record_ping(uid, mode)
            return JSONResponse({"ok": True})
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

    @mcp.custom_route("/usage", methods=["GET"], include_in_schema=False)
    async def usage(request: Request):
        if usage_store is None:
            return JSONResponse({"enabled": False}, status_code=503)
        token = request.query_params.get("token")
        if not validate_admin_token(token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(usage_store.summary())

    @mcp.custom_route("/usage/badge", methods=["GET"], include_in_schema=False)
    async def usage_badge(_: Request):
        """Returns a Shields.io compatible JSON for the user count."""
        if usage_store is None:
            return JSONResponse({"schemaVersion": 1, "label": "users", "message": "off", "color": "gray"})
        summary = usage_store.summary()
        count = summary.get("metrics", {}).get("total_unique_users", 0)
        return JSONResponse({
            "schemaVersion": 1,
            "label": "users",
            "message": str(count),
            "color": "blueviolet",
            "style": "flat-square",
        })
