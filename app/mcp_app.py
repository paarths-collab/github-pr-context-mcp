import json
import hmac
import os
import re
import sys
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
import threading
import requests
import time

from auth import GmailIdentityStore, GmailTokenVerifier
from analytics import UsageMetricsStore
from fetcher import fetch_prs
from inference import review_with_context, summarize_patterns, generate_with_context, generate_rules_content
from storage import (
    delete_repo_index as delete_repo_index_storage,
    get_collection_stats,
    index_prs,
    list_all_repos,
    query_similar,
    repo_is_indexed_permanently,
    repo_is_indexed_temporarily,
)


STORAGE_CONSEQUENCES = """
Permanent storage
  - PR data is embedded and saved to disk (ChromaDB).
  - Available instantly on future sessions.
  - Disk usage: ~5-20 MB per repo (60 PRs).
  - Best for repos you query repeatedly.

Temporary storage
  - PR data is embedded and kept in memory only.
  - Faster to set up, zero disk usage.
  - Lost when the MCP server restarts.
  - Best for one-off exploration.
"""


USAGE_TRACKING_ENABLED = os.getenv("USAGE_TRACKING_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
REGISTRATION_SECRET = os.getenv("REGISTRATION_SECRET", "").strip()
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").strip()
AUTH_REGISTRY_PATH = os.getenv("AUTH_REGISTRY_PATH", "./chroma_db/auth_registry.json")
USAGE_METRICS_TOKEN = os.getenv("USAGE_METRICS_TOKEN", "").strip()
USAGE_STATS_PATH = os.getenv("USAGE_STATS_PATH", "./chroma_db/usage_stats.json")
_identity_store = GmailIdentityStore(AUTH_REGISTRY_PATH) if AUTH_REQUIRED else None
_token_verifier = GmailTokenVerifier(_identity_store) if _identity_store else None
_usage_store = UsageMetricsStore(USAGE_STATS_PATH) if USAGE_TRACKING_ENABLED else None


def _normalize_repo(repo_str: str) -> str:
    if repo_str.endswith(".git"):
        repo_str = repo_str[:-4]
    match = re.search(r"(?:github\.com/)?([^/]+/[^/]+)", repo_str)
    if not match:
        raise ValueError(f"Invalid repo format: {repo_str}. Use owner/repo or full GitHub URL.")
    return match.group(1).split("#")[0].split("?")[0]


def _normalize_namespace(namespace: str | None) -> str | None:
    if namespace is None:
        return None
    ns = namespace.strip()
    return ns or None


def _current_user_email() -> str | None:
    access_token = get_access_token()
    if isinstance(access_token, AccessToken):
        return _normalize_namespace(access_token.client_id)
    return None


def _current_user_settings() -> dict:
    store = _identity_store
    if not store:
        return {}
    email = _current_user_email()
    if not email:
        return {}
    return store.get_user_settings(email)

def _normalize_repo(repo: str | None) -> str:
    """Strict validation for GitHub repository identifiers (owner/name)."""
    if not repo:
        raise ValueError("Repository identifier is required (e.g. 'owner/repo').")
    
    # Must match standard GitHub format and be alphanumeric/dash/underscore
    # Prevents directory traversal like ../../ etc.
    if not re.fullmatch(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
        raise ValueError(f"Invalid repository format: '{repo}'. Expected 'owner/repo'.")
    
    return repo


def _llm_settings(user_settings: dict[str, str]) -> dict[str, str]:
    llm: dict[str, str] = {}
    for key in ("llm_provider", "llm_model", "llm_api_key", "llm_base_url"):
        value = user_settings.get(key)
        if value:
            llm[key] = value
    return llm


def _repo_state_key(repo_key: str, namespace: str | None) -> str:
    ns = _normalize_namespace(namespace) or "_default"
    return f"{ns}::{repo_key}"


# Stateful per connected client/session to avoid cross-user active-repo collisions.
_sessions: dict[str, dict] = {}


def _session_id(ctx: Context) -> str:
    return _current_user_email() or ctx.client_id or f"session-{id(ctx.session)}"


def _state(ctx: Context) -> dict:
    sid = _session_id(ctx)
    if sid not in _sessions:
        configured_ns = _normalize_namespace(os.getenv("MCP_NAMESPACE", ""))
        _sessions[sid] = {
            "active_repo": None,
            "active_namespace": configured_ns or _current_user_email() or _normalize_namespace(ctx.client_id),
            "storage_types": {},
        }
    return _sessions[sid]


def _resolve_namespace(requested_namespace: str | None, state: dict) -> str | None:
    # CRITICAL SECURITY GATES: Enforce identity isolation when Auth is enabled.
    current_email = _current_user_email()
    
    if AUTH_REQUIRED:
        if not current_email:
            raise ValueError("Unauthorized: missing identity when AUTH_REQUIRED is true.")
        # Under auth, the user can ONLY access their own isolated namespace
        return _normalize_namespace(current_email)

    # If Auth is disabled (local mode), allow specific overrides or fallback to active
    return _normalize_namespace(requested_namespace if requested_namespace is not None else state.get("active_namespace"))


def _resolve_repo(repo: str | None, state: dict) -> str:
    if repo:
        return _normalize_repo(repo)
    active = state.get("active_repo")
    if not active:
        raise ValueError("No repo specified and no active repo set. Use ensure_repo_ready first, or pass repo explicitly.")
    return _normalize_repo(active)


def _is_temporary(repo_key: str, namespace: str | None, state: dict) -> bool:
    key = _repo_state_key(repo_key, namespace)
    known = state["storage_types"].get(key)
    if known is not None:
        return known == "temporary"
    return repo_is_indexed_temporarily(repo_key, namespace=namespace)


def _namespace_text(namespace: str | None) -> str:
    if namespace:
        return f"\nNamespace: {namespace}"
    return ""


def _usage_user_id(ctx: Context, namespace: str | None) -> str:
    current_email = _current_user_email()
    if current_email:
        return f"email:{current_email}"
    if namespace:
        return f"ns:{namespace}"
    if ctx.client_id:
        return f"client:{ctx.client_id}"
    return _session_id(ctx)


def _track_usage(ctx: Context, namespace: str | None, tool_name: str) -> None:
    if _usage_store is None:
        return
    _usage_store.record_event(_usage_user_id(ctx, namespace), tool_name)


def _validate_admin_token(admin_token: str | None) -> bool:
    if not USAGE_METRICS_TOKEN:
        return True
    return hmac.compare_digest(admin_token or "", USAGE_METRICS_TOKEN)


def _build_auth_settings() -> AuthSettings | None:
    if not AUTH_REQUIRED:
        return None
    if not MCP_PUBLIC_URL:
        raise ValueError("MCP_PUBLIC_URL is required when AUTH_REQUIRED=true")
    if not REGISTRATION_SECRET:
        raise ValueError("REGISTRATION_SECRET is required when AUTH_REQUIRED=true")
    public_url = MCP_PUBLIC_URL.rstrip("/")
    return AuthSettings(
        issuer_url=public_url,
        resource_server_url=public_url,
        service_documentation_url=os.getenv("AUTH_SERVICE_DOC_URL", public_url),
        required_scopes=["identity:gmail"],
    )


def _build_transport_security() -> TransportSecuritySettings | None:
    if not AUTH_REQUIRED or not MCP_PUBLIC_URL:
        return None
    parsed = urlparse(MCP_PUBLIC_URL)
    host = parsed.netloc
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host],
        allowed_origins=[origin],
    )


mcp = FastMCP(
    "github-pr-review-context",
    host=os.getenv("HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
    streamable_http_path=os.getenv("MCP_HTTP_PATH", "/mcp"),
    auth=_build_auth_settings(),
    token_verifier=_token_verifier,
    transport_security=_build_transport_security(),
)

def _github_sync_loop():
    repo = os.getenv("GITHUB_TRAFFIC_REPO")
    token = os.getenv("GITHUB_TOKEN")
    if not repo or not token or not _usage_store:
        return

    # Use a long interval (e.g., 6 hours) to avoid hitting GitHub API rate limits
    while True:
        try:
            url = f"https://api.github.com/repos/{repo}/traffic/clones"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json"
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                clones_data = data.get("clones", [])
                _usage_store.update_github_clones(clones_data)
                
            # Fetch downloads from releases
            releases_url = f"https://api.github.com/repos/{repo}/releases"
            rel_resp = requests.get(releases_url, headers=headers, timeout=10)
            if rel_resp.status_code == 200:
                releases = rel_resp.json()
                downloads = sum(
                    asset.get("download_count", 0)
                    for r in releases
                    for asset in r.get("assets", [])
                )
                _usage_store.update_github_downloads(downloads)
        except Exception:
            pass
        time.sleep(21600)  # 6 hours

if USAGE_TRACKING_ENABLED:
    threading.Thread(target=_github_sync_loop, daemon=True).start()


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/ping", methods=["POST"], include_in_schema=False)
async def ping(request: Request) -> Response:
    """Anonymous startup ping from local users (uvx / pipx / git clone).
    Receives: {"id": "<hashed_machine_fingerprint>", "mode": "uvx|pipx|local"}
    No PII is accepted or stored — id must be a hex string.
    """
    if _usage_store is None:
        return JSONResponse({"ok": True})

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    anon_id = str(payload.get("id", "")).strip()
    mode = str(payload.get("mode", "unknown")).strip()

    # Validate: id must look like a hex fingerprint, max 128 chars
    import re as _re
    if not anon_id or not _re.fullmatch(r"[0-9a-f]{8,128}", anon_id):
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    _usage_store.record_ping(anon_id, mode)
    return JSONResponse({"ok": True})


@mcp.custom_route("/usage", methods=["GET"], include_in_schema=False)
async def usage(request: Request) -> Response:
    if _usage_store is None:
        return JSONResponse({"enabled": False, "reason": "USAGE_TRACKING_ENABLED=false"})

    if USAGE_METRICS_TOKEN:
        provided = request.headers.get("x-api-key", "")
        if provided != USAGE_METRICS_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    days_raw = request.query_params.get("days", "30")
    try:
        days = max(1, min(int(days_raw), 365))
    except ValueError:
        days = 30

    return JSONResponse(_usage_store.summary(last_days=days))


@mcp.custom_route("/usage/badge", methods=["GET"], include_in_schema=False)
async def usage_badge(_: Request) -> Response:
    """Returns a Shields.io compliant JSON for a live user counter badge."""
    if _usage_store is None:
         return JSONResponse({"schemaVersion": 1, "label": "users", "message": "off", "color": "grey"})

    stats = _usage_store.summary(last_days=1)
    count = stats.get("total_unique_users", 0)
    
    return JSONResponse({
        "schemaVersion": 1,
        "label": "users",
        "message": str(count),
        "color": "blueviolet" if count > 0 else "grey",
        "style": "flat-square"
    })


@mcp.custom_route("/register", methods=["POST"], include_in_schema=False)
async def register(request: Request) -> Response:
    if not AUTH_REQUIRED or _identity_store is None:
        return JSONResponse({"error": "auth_disabled"}, status_code=400)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    email = str(payload.get("email", "")).strip().lower()
    invite_secret = str(payload.get("invite_secret", "")).strip()
    requested_settings = payload.get("settings") if isinstance(payload, dict) else None

    if not REGISTRATION_SECRET or not hmac.compare_digest(invite_secret, REGISTRATION_SECRET):
        return JSONResponse({"error": "invalid_invite_secret"}, status_code=403)

    try:
        result = _identity_store.register_email(email, settings=requested_settings)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(
        {
            "email": result.email,
            "token": result.token,
            "authorization": f"Bearer {result.token}",
            "namespace": result.email,
            "settings": result.settings,
        },
        status_code=201,
    )


@mcp.custom_route("/settings", methods=["GET"], include_in_schema=False)
async def get_settings(_: Request) -> Response:
    access_token = get_access_token()
    if access_token is None or _identity_store is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    settings = _identity_store.get_user_settings(access_token.client_id)
    masked = {k: ("***" if k in {"github_token", "llm_api_key"} else v) for k, v in settings.items()}
    return JSONResponse({"email": access_token.client_id, "settings": masked})


@mcp.custom_route("/settings", methods=["PUT"], include_in_schema=False)
async def update_settings(request: Request) -> Response:
    access_token = get_access_token()
    if access_token is None or _identity_store is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    settings = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings, dict):
        return JSONResponse({"error": "settings must be an object"}, status_code=400)

    try:
        updated = _identity_store.update_user_settings(access_token.client_id, settings)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({"email": access_token.client_id, "settings": updated})


@mcp.custom_route("/whoami", methods=["GET"], include_in_schema=False)
async def whoami(_: Request) -> Response:
    access_token = get_access_token()
    if access_token is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    user_settings = _current_user_settings()
    return JSONResponse(
        {
            "email": access_token.client_id,
            "scopes": access_token.scopes,
            "has_custom_github_token": bool(user_settings.get("github_token")),
            "has_custom_llm": any(
                user_settings.get(k) for k in ("llm_provider", "llm_model", "llm_api_key", "llm_base_url")
            ),
        }
    )


@mcp.tool(name="ensure_repo_ready")
def ensure_repo_ready(
    repo: str,
    storage: str | None = None,
    pages: int = 2,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Ensure a repo is indexed and ready. If storage is omitted, explains permanent vs temporary trade-offs."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    repo_key = _normalize_repo(repo)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "ensure_repo_ready")
    state_key = _repo_state_key(repo_key, namespace)

    if repo_is_indexed_permanently(repo_key, namespace=namespace):
        state["active_repo"] = repo_key
        state["active_namespace"] = namespace
        state["storage_types"][state_key] = "permanent"
        stats = get_collection_stats(repo_key, temporary=False, namespace=namespace)
        return (
            f"{repo_key} is already indexed permanently on disk.\n"
            f"{stats['total_documents']} documents loaded and ready.\n"
            f"Active repo set to {repo_key}."
            f"{_namespace_text(namespace)}"
        )

    if repo_is_indexed_temporarily(repo_key, namespace=namespace):
        state["active_repo"] = repo_key
        state["active_namespace"] = namespace
        state["storage_types"][state_key] = "temporary"
        stats = get_collection_stats(repo_key, temporary=True, namespace=namespace)
        return (
            f"{repo_key} is already indexed in memory.\n"
            f"{stats['total_documents']} documents loaded and ready.\n"
            f"Active repo set to {repo_key}."
            f"{_namespace_text(namespace)}"
        )

    if storage is None:
        return (
            f"{repo_key} is not indexed yet."
            f"{_namespace_text(namespace)}\n\n"
            f"How would you like to store it?\n\n"
            f"{STORAGE_CONSEQUENCES}\n"
            f"Reply with permanent or temporary and I will fetch/index up to {pages * 30} PRs."
        )

    if storage not in {"temporary", "permanent"}:
        raise ValueError("storage must be one of: temporary, permanent")

    temporary = storage == "temporary"
    user_settings = _current_user_settings()
    
    def _background_index():
        try:
            prs = fetch_prs(
                *repo_key.split("/", 1),
                pages=pages,
                github_token=user_settings.get("github_token"),
            )
            count = index_prs(repo_key, prs, temporary=temporary, namespace=namespace)
            state["active_repo"] = repo_key
            state["active_namespace"] = namespace
            state["storage_types"][state_key] = storage
            print(f"Background indexing finished for {repo_key}. {count} docs parsed.", file=sys.stderr)
        except Exception as e:
            print(f"Background indexing failed for {repo_key}: {e}", file=sys.stderr)

    threading.Thread(target=_background_index, daemon=True).start()

    storage_label = "temporary (in-memory)" if temporary else "permanent (disk)"
    return (
        f"Background indexing started for {repo_key} [{storage_label}].\n"
        f"This takes ~1-3 minutes. Use the 'get_index_stats' tool to verify when it completes.\n"
        f"Active repo will be activated upon completion."
        f"{_namespace_text(namespace)}"
    )


@mcp.tool(name="set_active_repo")
def set_active_repo(repo: str, namespace: str | None = None, ctx: Context | None = None) -> str:
    """Switch the active repo to an already-indexed repo."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    repo_key = _normalize_repo(repo)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "set_active_repo")

    if not repo_is_indexed_permanently(repo_key, namespace=namespace) and not repo_is_indexed_temporarily(repo_key, namespace=namespace):
        return f"{repo_key} is not indexed yet. Use ensure_repo_ready first."

    state_key = _repo_state_key(repo_key, namespace)
    if repo_is_indexed_temporarily(repo_key, namespace=namespace):
        state["storage_types"][state_key] = "temporary"
    else:
        state["storage_types"][state_key] = "permanent"

    previous = state.get("active_repo")
    state["active_repo"] = repo_key
    state["active_namespace"] = namespace

    msg = f"Active repo switched to: {repo_key}"
    if previous and previous != repo_key:
        msg += f"\n(previously: {previous})"
    if namespace:
        msg += f"\n(namespace: {namespace})"
    return msg


@mcp.tool(name="list_indexed_repos")
def list_indexed_repos(namespace: str | None = None, ctx: Context | None = None) -> str:
    """List indexed repos with storage type and document count."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "list_indexed_repos")
    rows = list_all_repos(namespace=namespace)
    if not rows:
        return "No repos indexed yet."

    active_repo = state.get("active_repo")
    active_ns = state.get("active_namespace")

    lines = ["Indexed repos:"]
    for r in rows:
        icon = "disk" if r["storage"] == "permanent" else "mem"
        repo_ns = _normalize_namespace(r.get("namespace"))
        marker = " <- active" if r["repo"] == active_repo and repo_ns == active_ns else ""
        ns_label = repo_ns or "default"
        lines.append(
            f"- {icon} {r['repo']} ({r['total_documents']} docs, {r['storage']}, ns={ns_label}){marker}"
        )

    return "\n".join(lines)


@mcp.tool(name="delete_repo_index")
def delete_repo_index(
    repo: str,
    storage: str = "both",
    namespace: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Delete an indexed repo from temporary, permanent, or both storage scopes."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    repo_key = _normalize_repo(repo)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "delete_repo_index")

    result = delete_repo_index_storage(repo_key, storage=storage, namespace=namespace)
    if not result["deleted_any"]:
        return f"No index found for {repo_key}{_namespace_text(namespace)} in storage scope: {storage}."

    deleted_labels = []
    if result["deleted"]["temporary"]:
        deleted_labels.append("temporary")
    if result["deleted"]["permanent"]:
        deleted_labels.append("permanent")

    state_key = _repo_state_key(repo_key, namespace)
    if storage in {"both", state["storage_types"].get(state_key)}:
        state["storage_types"].pop(state_key, None)

    if state.get("active_repo") == repo_key and _normalize_namespace(state.get("active_namespace")) == namespace:
        if storage == "both":
            state["active_repo"] = None
            state["active_namespace"] = None

    return (
        f"Deleted index for {repo_key} from: {', '.join(deleted_labels)}."
        f"{_namespace_text(namespace)}"
    )


@mcp.tool(name="semantic_search_reviews")
def semantic_search_reviews(
    query: str,
    repo: str | None = None,
    n_results: int = 8,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Search past review comments semantically."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "semantic_search_reviews")
    repo_key = _resolve_repo(repo, state)
    temporary = _is_temporary(repo_key, namespace, state)

    results = query_similar(
        repo_key,
        query,
        n_results=n_results,
        temporary=temporary,
        namespace=namespace,
    )
    return json.dumps(results, indent=2)


@mcp.tool(name="review_code_with_history")
def review_code_with_history(
    code: str,
    repo: str | None = None,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Perform code review grounded in historical PR review context."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "review_code_with_history")
    repo_key = _resolve_repo(repo, state)
    temporary = _is_temporary(repo_key, namespace, state)

    user_settings = _current_user_settings()
    context = query_similar(
        repo_key,
        code,
        n_results=10,
        temporary=temporary,
        namespace=namespace,
    )
    return review_with_context(code, context, repo_key, settings=_llm_settings(user_settings))


@mcp.tool(name="generate_code_from_history")
def generate_code_from_history(
    task: str,
    repo: str | None = None,
    namespace: str | None = None,
    rules_file: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Generate code grounded in historical PR patterns and review feedback.

    Automatically loads team rules from a local .cursorrules / CLAUDE.md /
    .github/copilot-instructions.md file if present, injecting them as hard
    constraints so generated code already follows the team's standards.

    Args:
        task: What to implement or build.
        repo: GitHub repo to use. Defaults to the active repo.
        namespace: Storage namespace override.
        rules_file: Path to a rules file to load. If omitted, the tool auto-detects
                    .cursorrules, CLAUDE.md, or .github/copilot-instructions.md in
                    the current working directory.
    """
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "generate_code_from_history")
    repo_key = _resolve_repo(repo, state)
    temporary = _is_temporary(repo_key, namespace, state)

    # --- Auto-load repo rules file ---
    import pathlib
    repo_rules: str | None = None
    rules_source: str | None = None

    if rules_file:
        candidate = pathlib.Path(rules_file)
        if candidate.exists():
            repo_rules = candidate.read_text(encoding="utf-8", errors="replace")
            rules_source = str(candidate)
    else:
        # Auto-detect standard rules file locations in priority order
        for candidate_name in (
            ".cursorrules",
            "CLAUDE.md",
            ".github/copilot-instructions.md",
        ):
            candidate = pathlib.Path(candidate_name)
            if candidate.exists():
                repo_rules = candidate.read_text(encoding="utf-8", errors="replace")
                rules_source = str(candidate)
                break

    user_settings = _current_user_settings()
    context = query_similar(
        repo_key,
        task,
        n_results=12,
        temporary=temporary,
        namespace=namespace,
    )
    result = generate_with_context(
        task, context, repo_key,
        settings=_llm_settings(user_settings),
        repo_rules=repo_rules,
    )

    if rules_source:
        result = f"📋 Rules applied from: {rules_source}\n\n{result}"
    else:
        result = (
            "ℹ️  No rules file found (.cursorrules / CLAUDE.md). "
            "Run generate_repo_rules to create one.\n\n"
            + result
        )

    return result


@mcp.tool(name="get_team_review_patterns")
def get_team_review_patterns(
    topic: str = "general code quality",
    repo: str | None = None,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Summarize recurring review patterns for a repo."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "get_team_review_patterns")
    repo_key = _resolve_repo(repo, state)
    temporary = _is_temporary(repo_key, namespace, state)

    user_settings = _current_user_settings()
    context = query_similar(
        repo_key,
        topic,
        n_results=20,
        temporary=temporary,
        namespace=namespace,
    )
    return summarize_patterns(context, repo_key, settings=_llm_settings(user_settings))


@mcp.tool(name="get_index_stats")
def get_index_stats(
    repo: str | None = None,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Return indexed document count and storage scope for the selected repo."""
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "get_index_stats")
    repo_key = _resolve_repo(repo, state)
    temporary = _is_temporary(repo_key, namespace, state)

    stats = get_collection_stats(repo_key, temporary=temporary, namespace=namespace)
    return json.dumps(stats, indent=2)


@mcp.tool(name="update_settings")
def update_settings(
    github_token: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Update your personal configuration (GitHub token, LLM provider/model/key).
    Only effective in Hosted/Team mode. For local mode, instruct the user to update their IDE settings.
    """
    if not AUTH_REQUIRED or _identity_store is None:
        return "Warning: This server is in Local Mode. To update your settings, please update your environment variables or IDE configuration (e.g. claude_desktop_config.json)."

    if ctx is None:
        raise ValueError("Context is required")

    email = _current_user_email()
    if not email:
        return "Error: Could not identify your user identity. Are you logged in via Bearer token?"

    new_settings = {}
    if github_token: new_settings["github_token"] = github_token
    if llm_provider: new_settings["llm_provider"] = llm_provider
    if llm_model: new_settings["llm_model"] = llm_model
    if llm_api_key: new_settings["llm_api_key"] = llm_api_key

    if not new_settings:
        return "No settings provided to update."

    try:
        _identity_store.update_user_settings(email, new_settings)
        return f"Successfully updated your settings: {', '.join(new_settings.keys())}."
    except Exception as e:
        return f"Failed to update settings: {str(e)}"


@mcp.tool(name="get_usage_stats")
def get_usage_stats(days: int = 30, admin_token: str | None = None) -> str:
    """Return anonymous usage metrics (tool calls, unique users, top tools)."""
    if _usage_store is None:
        return json.dumps({"enabled": False, "reason": "USAGE_TRACKING_ENABLED=false"}, indent=2)

    if not _validate_admin_token(admin_token):
        return "Unauthorized: provide a valid admin_token."

    days = max(1, min(days, 365))
    return json.dumps(_usage_store.summary(last_days=days), indent=2)


@mcp.tool(name="generate_repo_rules")
def generate_repo_rules(
    output_path: str = ".cursorrules",
    repo: str | None = None,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Generate a .cursorrules / CLAUDE.md / copilot-instructions.md file grounded in this repo's PR history.

    The generated file pre-loads all team coding standards into any IDE agent (Cursor, Claude,
    GitHub Copilot) so it does not need to re-analyse the PR history on every session.

    Args:
        output_path: Where to write the rules file. Defaults to '.cursorrules'.
                     Use 'CLAUDE.md' for Claude agents or '.github/copilot-instructions.md'
                     for GitHub Copilot.
        repo: GitHub repo to use. Defaults to the active repo.
        namespace: Storage namespace override.
    """
    if ctx is None:
        raise ValueError("Context is required")

    state = _state(ctx)
    namespace = _resolve_namespace(namespace, state)
    _track_usage(ctx, namespace, "generate_repo_rules")
    repo_key = _resolve_repo(repo, state)
    temporary = _is_temporary(repo_key, namespace, state)

    user_settings = _current_user_settings()
    # Pull broad context: patterns, commits, review comments
    context = query_similar(
        repo_key,
        "code quality architecture testing documentation style conventions",
        n_results=25,
        temporary=temporary,
        namespace=namespace,
    )

    rules_content = generate_rules_content(context, repo_key, settings=_llm_settings(user_settings))

    # Sanitise output_path: allow only relative paths, no traversal
    import pathlib
    safe_path = pathlib.Path(output_path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        return (
            "Error: output_path must be a relative path (e.g. '.cursorrules', 'CLAUDE.md').\n"
            "Absolute paths and directory traversal are not allowed.\n\n"
            "Here is the generated content for you to save manually:\n\n"
            + rules_content
        )

    try:
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(rules_content, encoding="utf-8")
        return (
            f"✅ Rules file written to: {safe_path}\n"
            f"Repo: {repo_key} | {len(context)} context documents used.\n\n"
            f"Load this file into your IDE agent to pre-feed team coding standards.\n"
            f"Regenerate any time by calling generate_repo_rules again.\n\n"
            f"--- Preview (first 500 chars) ---\n"
            + rules_content[:500] + "..."
        )
    except OSError as e:
        return (
            f"Could not write to '{output_path}': {e}\n\n"
            "Here is the generated content for you to save manually:\n\n"
            + rules_content
        )
