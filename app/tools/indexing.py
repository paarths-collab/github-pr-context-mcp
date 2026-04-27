import sys
import threading
from mcp.server.fastmcp import Context
from fetcher import fetch_prs
from storage import (
    delete_repo_index as delete_repo_index_storage,
    get_collection_stats,
    index_prs,
    list_all_repos,
    repo_is_indexed_permanently,
    repo_is_indexed_temporarily,
)
from app.state import (
    normalize_repo,
    normalize_namespace,
    resolve_namespace,
    repo_state_key,
    get_state,
    namespace_text,
    current_user_settings,
    track_usage,
    resolve_repo
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

def register_indexing_tools(mcp):
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

        state = get_state(ctx)
        repo_key = normalize_repo(repo)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "ensure_repo_ready")
        state_key = repo_state_key(repo_key, namespace)

        if repo_is_indexed_permanently(repo_key, namespace=namespace):
            state["active_repo"] = repo_key
            state["active_namespace"] = namespace
            state["storage_types"][state_key] = "permanent"
            stats = get_collection_stats(repo_key, temporary=False, namespace=namespace)
            return (
                f"{repo_key} is already indexed permanently on disk.\n"
                f"{stats['total_documents']} documents loaded and ready.\n"
                f"Active repo set to {repo_key}."
                f"{namespace_text(namespace)}"
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
                f"{namespace_text(namespace)}"
            )

        if storage is None:
            return (
                f"{repo_key} is not indexed yet."
                f"{namespace_text(namespace)}\n\n"
                f"How would you like to store it?\n\n"
                f"{STORAGE_CONSEQUENCES}\n"
                f"Reply with permanent or temporary and I will fetch/index up to {pages * 30} PRs."
            )

        if storage not in {"temporary", "permanent"}:
            raise ValueError("storage must be one of: temporary, permanent")

        temporary = storage == "temporary"
        user_settings = current_user_settings()
        
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
            f"{namespace_text(namespace)}"
        )

    @mcp.tool(name="set_active_repo")
    def set_active_repo(repo: str, namespace: str | None = None, ctx: Context | None = None) -> str:
        """Switch the active repo to an already-indexed repo."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        repo_key = normalize_repo(repo)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "set_active_repo")

        if not repo_is_indexed_permanently(repo_key, namespace=namespace) and not repo_is_indexed_temporarily(repo_key, namespace=namespace):
            return f"{repo_key} is not indexed yet. Use ensure_repo_ready first."

        state_key = repo_state_key(repo_key, namespace)
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

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "list_indexed_repos")
        rows = list_all_repos(namespace=namespace)
        if not rows:
            return "No repos indexed yet."

        active_repo = state.get("active_repo")
        active_ns = state.get("active_namespace")

        lines = ["Indexed repos:"]
        for r in rows:
            icon = "disk" if r["storage"] == "permanent" else "mem"
            repo_ns = normalize_namespace(r.get("namespace"))
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

        state = get_state(ctx)
        repo_key = normalize_repo(repo)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "delete_repo_index")

        result = delete_repo_index_storage(repo_key, storage=storage, namespace=namespace)
        if not result["deleted_any"]:
            return f"No index found for {repo_key}{namespace_text(namespace)} in storage scope: {storage}."

        deleted_labels = []
        if result["deleted"]["temporary"]:
            deleted_labels.append("temporary")
        if result["deleted"]["permanent"]:
            deleted_labels.append("permanent")

        state_key = repo_state_key(repo_key, namespace)
        if storage in {"both", state["storage_types"].get(state_key)}:
            state["storage_types"].pop(state_key, None)

        if state.get("active_repo") == repo_key and normalize_namespace(state.get("active_namespace")) == namespace:
            if storage == "both":
                state["active_repo"] = None
                state["active_namespace"] = None

        return (
            f"Deleted index for {repo_key} from: {', '.join(deleted_labels)}."
            f"{namespace_text(namespace)}"
        )

    @mcp.tool(name="get_index_stats")
    def get_index_stats(
        repo: str | None = None,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Return indexed document count and storage scope for the selected repo."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "get_index_stats")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)

        import json
        stats = get_collection_stats(repo_key, temporary=temporary, namespace=namespace)
        return json.dumps(stats, indent=2)
