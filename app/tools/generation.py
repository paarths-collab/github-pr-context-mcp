import asyncio
from mcp.server.fastmcp import Context
from app.retrieval import DEFAULT_BUDGET, GUIDANCE_SUFFIX, evidence_response
from app.state import (
    resolve_namespace,
    get_state,
    track_usage,
    resolve_repo,
    is_temporary
)


def register_generation_tools(mcp):
    @mcp.tool(name="generate_code_from_history")
    async def generate_code_from_history(
        task: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
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

        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=task,
            n_results=15,
            budget_tokens=budget_tokens,
            legacy_key="historical_context",
            extra={
                "repo": repo_key,
                "task": task,
                "instruction": (
                    "Generate code for the requested task. Use the provided historical context "
                    "to match the team's architectural patterns, naming conventions, and common "
                    "reviewer feedback." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="get_repo_rules_material")
    async def get_repo_rules_material(
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
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
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query="code quality architecture testing documentation style conventions formatting patterns",
            n_results=35,
            budget_tokens=budget_tokens,
            legacy_key="rules_material",
            extra={
                "repo": repo_key,
                "instruction": (
                    "Analyze these historical review comments and PR descriptions. "
                    "Synthesize them into a comprehensive .cursorrules or CLAUDE.md file "
                    "that documents the team's coding standards, testing requirements, "
                    "and preferred architectural patterns. Only record a convention when the "
                    "evidence is consistent and still matches the current code; mention "
                    "candidate_conflicts and coverage limits rather than hiding them."
                    + GUIDANCE_SUFFIX
                ),
            },
        )
