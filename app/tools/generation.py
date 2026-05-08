import json
import asyncio
from mcp.server.fastmcp import Context
from storage import query_similar, get_collection_stats
from app.state import (
    resolve_namespace,
    get_state,
    track_usage,
    resolve_repo,
    is_temporary
)

async def _get_stale_warning(repo_key: str, temporary: bool, namespace: str | None) -> str | None:
    """Check if the index is stale and return a warning string if so."""
    stats = await asyncio.to_thread(get_collection_stats, repo_key, temporary=temporary, namespace=namespace)
    if stats.get("is_stale"):
        days = stats.get("days_old", 30)
        return f"Index is {days} days old. We recommend running 'ensure_repo_ready' to refresh patterns."
    return None

def register_generation_tools(mcp):
    @mcp.tool(name="generate_code_from_history")
    async def generate_code_from_history(
        task: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical coding patterns and review feedback for a task.
        The IDE agent should use these to generate code grounded in team standards.
        """
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "generate_code_from_history")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)

        context = await asyncio.to_thread(
            query_similar,
            repo_key,
            task,
            n_results=15,
            temporary=temporary,
            namespace=namespace,
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "task": task,
            "historical_context": context,
            "instruction": (
                "Generate code for the requested task. Use the provided historical context "
                "to match the team's architectural patterns, naming conventions, and common "
                "reviewer feedback."
            )
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="get_repo_rules_material")
    async def get_repo_rules_material(
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve a high-density summary of historical reviews and standards.
        The IDE agent should use this to write or update .cursorrules / CLAUDE.md.
        """
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "get_repo_rules_material")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)

        # Query for a broad set of quality and style indicators
        context = await asyncio.to_thread(
            query_similar,
            repo_key,
            "code quality architecture testing documentation style conventions formatting patterns",
            n_results=35,
            temporary=temporary,
            namespace=namespace,
        )

        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "rules_material": context,
            "instruction": (
                "Analyze these historical review comments and PR descriptions. "
                "Synthesize them into a comprehensive .cursorrules or CLAUDE.md file "
                "that documents the team's coding standards, testing requirements, "
                "and preferred architectural patterns."
            )
        }
        if warning:
            response["warning"] = warning

        return json.dumps(response, indent=2)
