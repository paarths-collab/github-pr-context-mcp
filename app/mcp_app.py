import sys
import os
import threading
from mcp.server.fastmcp import FastMCP

from app.state import (
    build_auth_settings,
    token_verifier,
    build_transport_security,
    usage_store
)
from app.tools.indexing import register_indexing_tools
from app.tools.analysis import register_analysis_tools
from app.tools.generation import register_generation_tools
from app.tools.admin import register_admin_tools
from app.tools.github_auth import register_github_auth_tools
from app.routes.http import register_http_routes

# Initialize FastMCP
mcp = FastMCP(
    "github-pr-review-context",
    host=os.getenv("HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
    streamable_http_path=os.getenv("MCP_HTTP_PATH", "/mcp"),
    auth=build_auth_settings(),
    token_verifier=token_verifier,
    transport_security=build_transport_security(),
)

# Register Tools
register_indexing_tools(mcp)
register_analysis_tools(mcp)
register_generation_tools(mcp)
register_admin_tools(mcp)
register_github_auth_tools(mcp)

# Register HTTP Routes
register_http_routes(mcp)

# Background Sync Loop (GitHub Traffic)
def _github_sync_loop():
    repo = os.getenv("GITHUB_TRAFFIC_REPO")
    # Analytics credentials must not share the developer's PR-context token.
    token = os.getenv("GITHUB_TRAFFIC_TOKEN")
    if not repo or not token or not usage_store:
        return
    
    owner, name = repo.split("/", 1)
    print(f"Starting GitHub traffic sync for {repo}...", file=sys.stderr, flush=True)
    while True:
        try:
            usage_store.sync_github_traffic(owner, name, token)
        except Exception as e:
            print(f"GitHub traffic sync failed: {e}", file=sys.stderr, flush=True)
        import time
        time.sleep(3600 * 12)  # Sync every 12 hours

if os.getenv("GITHUB_TRAFFIC_REPO"):
    threading.Thread(target=_github_sync_loop, daemon=True).start()
