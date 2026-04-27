import hmac
import os
import re
import sys
from mcp.server.fastmcp import Context
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from urllib.parse import urlparse

from auth import GmailIdentityStore, GmailTokenVerifier
from analytics import UsageMetricsStore
from storage import (
    repo_is_indexed_permanently,
    repo_is_indexed_temporarily,
)

# --- Configuration Constants ---
USAGE_TRACKING_ENABLED = os.getenv("USAGE_TRACKING_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").strip().lower() in {"1", "true", "yes", "on"}
REGISTRATION_SECRET = os.getenv("REGISTRATION_SECRET", "").strip()
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").strip()
AUTH_REGISTRY_PATH = os.getenv("AUTH_REGISTRY_PATH", "./chroma_db/auth_registry.json")
USAGE_METRICS_TOKEN = os.getenv("USAGE_METRICS_TOKEN", "").strip()
USAGE_STATS_PATH = os.getenv("USAGE_STATS_PATH", "./chroma_db/usage_stats.json")

# --- Globals ---
identity_store = GmailIdentityStore(AUTH_REGISTRY_PATH) if AUTH_REQUIRED else None
token_verifier = GmailTokenVerifier(identity_store) if identity_store else None
usage_store = UsageMetricsStore(USAGE_STATS_PATH) if USAGE_TRACKING_ENABLED else None

# Stateful per connected client/session
_sessions: dict[str, dict] = {}

# --- Helper Functions ---
def normalize_repo(repo: str | None) -> str:
    """Strict validation for GitHub repository identifiers (owner/name)."""
    if not repo:
        raise ValueError("Repository identifier is required (e.g. 'owner/repo').")
    
    # Handle full URLs
    if repo.endswith(".git"):
        repo = repo[:-4]
    match = re.search(r"(?:github\.com/)?([^/]+/[^/]+)", repo)
    if match:
        repo = match.group(1).split("#")[0].split("?")[0]

    if not re.fullmatch(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
        raise ValueError(f"Invalid repository format: '{repo}'. Expected 'owner/repo'.")
    
    return repo

def normalize_namespace(namespace: str | None) -> str | None:
    if namespace is None:
        return None
    ns = namespace.strip()
    return ns or None

def current_user_email() -> str | None:
    access_token = get_access_token()
    if isinstance(access_token, AccessToken):
        return normalize_namespace(access_token.client_id)
    return None

def current_user_settings() -> dict:
    store = identity_store
    if not store:
        return {}
    email = current_user_email()
    if not email:
        return {}
    return store.get_user_settings(email)

def llm_settings(user_settings: dict[str, str]) -> dict[str, str]:
    llm: dict[str, str] = {}
    for key in ("llm_provider", "llm_model", "llm_api_key", "llm_base_url"):
        value = user_settings.get(key)
        if value:
            llm[key] = value
    return llm

def repo_state_key(repo_key: str, namespace: str | None) -> str:
    ns = normalize_namespace(namespace) or "_default"
    return f"{ns}::{repo_key}"

def session_id(ctx: Context) -> str:
    return current_user_email() or ctx.client_id or f"session-{id(ctx.session)}"

def get_state(ctx: Context) -> dict:
    sid = session_id(ctx)
    if sid not in _sessions:
        configured_ns = normalize_namespace(os.getenv("MCP_NAMESPACE", ""))
        _sessions[sid] = {
            "active_repo": None,
            "active_namespace": configured_ns or current_user_email() or normalize_namespace(ctx.client_id),
            "storage_types": {},
        }
    return _sessions[sid]

def resolve_namespace(requested_namespace: str | None, state: dict) -> str | None:
    current_email = current_user_email()
    if AUTH_REQUIRED:
        if not current_email:
            raise ValueError("Unauthorized: missing identity when AUTH_REQUIRED is true.")
        return normalize_namespace(current_email)
    return normalize_namespace(requested_namespace if requested_namespace is not None else state.get("active_namespace"))

def resolve_repo(repo: str | None, state: dict) -> str:
    if repo:
        return normalize_repo(repo)
    active = state.get("active_repo")
    if not active:
        raise ValueError("No repo specified and no active repo set. Use ensure_repo_ready first, or pass repo explicitly.")
    return normalize_repo(active)

def is_temporary(repo_key: str, namespace: str | None, state: dict) -> bool:
    key = repo_state_key(repo_key, namespace)
    known = state["storage_types"].get(key)
    if known is not None:
        return known == "temporary"
    return repo_is_indexed_temporarily(repo_key, namespace=namespace)

def namespace_text(namespace: str | None) -> str:
    if namespace:
        return f"\nNamespace: {namespace}"
    return ""

def usage_user_id(ctx: Context, namespace: str | None) -> str:
    current_email = current_user_email()
    if current_email:
        return f"email:{current_email}"
    if namespace:
        return f"ns:{namespace}"
    if ctx.client_id:
        return f"client:{ctx.client_id}"
    return session_id(ctx)

def track_usage(ctx: Context, namespace: str | None, tool_name: str) -> None:
    if usage_store is None:
        return
    usage_store.record_event(usage_user_id(ctx, namespace), tool_name)

def validate_admin_token(admin_token: str | None) -> bool:
    if not USAGE_METRICS_TOKEN:
        return True
    return hmac.compare_digest(admin_token or "", USAGE_METRICS_TOKEN)

def build_auth_settings() -> AuthSettings | None:
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

def build_transport_security() -> TransportSecuritySettings | None:
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
