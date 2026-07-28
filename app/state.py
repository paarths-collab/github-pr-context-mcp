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
from dotenv import load_dotenv

from auth import (
    GmailIdentityStore,
    GmailTokenVerifier,
    GitHubAuthorizationRequired,
    GitHubDeviceFlowService,
    build_local_github_auth_service,
)
from analytics import UsageMetricsStore


load_dotenv()

# --- Configuration Constants ---
USAGE_TRACKING_ENABLED = os.getenv("USAGE_TRACKING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").strip().lower() in {"1", "true", "yes", "on"}
MCP_RUNTIME_MODE = os.getenv("GITHUB_PR_CONTEXT_RUNTIME", "").strip().lower()
# Device Flow is only safe in the local stdio process that owns its OS vault.
# Unknown/direct launch modes deliberately fail closed rather than sharing a
# machine credential through an accidentally exposed HTTP server.
LOCAL_GITHUB_DEVICE_FLOW_ENABLED = MCP_RUNTIME_MODE == "local" and not AUTH_REQUIRED
REGISTRATION_SECRET = os.getenv("REGISTRATION_SECRET", "").strip()
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").strip()
AUTH_REGISTRY_PATH = os.getenv("AUTH_REGISTRY_PATH", "./chroma_db/auth_registry.json")
USAGE_METRICS_TOKEN = os.getenv("USAGE_METRICS_TOKEN", "").strip()
USAGE_STATS_PATH = os.getenv("USAGE_STATS_PATH", "./chroma_db/usage_stats.json")

# --- Globals ---
identity_store = GmailIdentityStore(AUTH_REGISTRY_PATH) if AUTH_REQUIRED else None
token_verifier = GmailTokenVerifier(identity_store) if identity_store else None
usage_store = UsageMetricsStore(USAGE_STATS_PATH) if USAGE_TRACKING_ENABLED else None
# Local GitHub authorization deliberately uses the operating-system vault, never
# the hosted SQLite settings store. Do not even construct the local credential
# service in a non-stdio runtime: a hosted process must use a future tenant-aware
# GitHub App backend instead of a single machine's vault.
github_auth_service: GitHubDeviceFlowService | None = (
    build_local_github_auth_service() if LOCAL_GITHUB_DEVICE_FLOW_ENABLED else None
)

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
    """Retrieves settings for the current authenticated user (Hosted mode)."""
    email = current_user_email()
    if email and identity_store:
        return identity_store.get_user_settings(email)
    return {}


async def get_github_access_token() -> str:
    """Resolve a GitHub token through the local Device Flow credential boundary."""
    if not LOCAL_GITHUB_DEVICE_FLOW_ENABLED or github_auth_service is None:
        raise GitHubAuthorizationRequired(
            "GitHub PR retrieval is disabled outside the local stdio MCP. This "
            "v0.3 server does not use a shared local credential; deploy a "
            "tenant-aware GitHub App backend before enabling hosted retrieval."
        )
    return await github_auth_service.get_access_token()

def repo_state_key(repo_key: str, namespace: str | None) -> str:
    ns = normalize_namespace(namespace)
    scope = "default:" if ns is None else f"namespace:{ns}"
    return f"{scope}::{repo_key}"

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

import subprocess

def detect_repo_from_git(path: str | None = None) -> str | None:
    """Detect owner/repo from git remote origin in the given path or CWD."""
    try:
        cwd = os.path.dirname(path) if path and os.path.isfile(path) else (path or os.getcwd())
        if not os.path.exists(cwd):
            return None

        # Check if git is even installed
        try:
            subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Git not found, fall back silently
            return None

        # Check if we are in a git repo
        try:
            subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        except subprocess.CalledProcessError:
            # Not a git repo
            return None

        output = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            timeout=3
        ).decode().strip()
        
        # Handle SSH and HTTPS formats
        # SSH: git@github.com:owner/repo.git
        # HTTPS: https://github.com/owner/repo.git
        if output.endswith(".git"):
            output = output[:-4]
        
        # Capture the last two parts: owner/repo
        match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+)$", output)
        if match:
            return match.group(1)
            
    except Exception as e:
        # Log to stderr to avoid breaking MCP protocol on stdout
        print(f"[*] Git auto-detection failed for path '{path}': {e}", file=sys.stderr)
        
    return None

def resolve_namespace(requested_namespace: str | None, state: dict) -> str | None:
    current_email = current_user_email()
    if AUTH_REQUIRED:
        if not current_email:
            raise ValueError("Unauthorized: missing identity when AUTH_REQUIRED is true.")
        return normalize_namespace(current_email)
    return normalize_namespace(requested_namespace if requested_namespace is not None else state.get("active_namespace"))

def resolve_repo(repo: str | None, state: dict, file_path: str | None = None) -> str:
    """
    Resolve repo with fallback chain:
    1. Explicit 'repo' argument
    2. Auto-detected repo from 'file_path' (multi-repo workspace support)
    3. Active repo in session state
    4. Auto-detected repo from CWD
    """
    if repo:
        return normalize_repo(repo)
    
    if file_path:
        detected = detect_repo_from_git(file_path)
        if detected:
            return normalize_repo(detected)

    active = state.get("active_repo")
    if active:
        return normalize_repo(active)
    
    detected_cwd = detect_repo_from_git()
    if detected_cwd:
        # Side effect: set active repo if detected from CWD to help future calls
        state["active_repo"] = detected_cwd
        return normalize_repo(detected_cwd)

    raise ValueError(
        "Could not determine repository. Please specify 'repo' explicitly (owner/name) "
        "or ensure you are running in a git repository with an 'origin' remote."
    )

def is_temporary(repo_key: str, namespace: str | None, state: dict) -> bool:
    key = repo_state_key(repo_key, namespace)
    known = state["storage_types"].get(key)
    is_temp = False
    
    if known is not None:
        is_temp = (known == "temporary")
    else:
        from storage import repo_is_indexed_temporarily

        is_temp = repo_is_indexed_temporarily(repo_key, namespace=namespace)

    if is_temp:
        # Update LRU (Item 10)
        lru = state.setdefault("temp_lru", [])
        if repo_key in lru:
            lru.remove(repo_key)
        lru.append(repo_key)
        
        if len(lru) > 5:
            from storage import delete_repo_index
            oldest = lru.pop(0)
            print(f"[*] LRU Eviction: Deleting oldest temp repo index: {oldest}", file=sys.stderr)
            delete_repo_index(oldest, storage="temporary", namespace=namespace)
            
    return is_temp

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
