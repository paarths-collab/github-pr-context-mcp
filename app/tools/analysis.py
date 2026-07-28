import json
import asyncio
from mcp.server.fastmcp import Context
from storage import query_similar, get_collection_stats
from app.state import (
    get_state,
    is_temporary,
    resolve_namespace,
    resolve_repo,
    track_usage,
)

async def _get_stale_warning(repo_key: str, temporary: bool, namespace: str | None) -> str | None:
    """Check if the index is stale and return a warning string if so."""
    stats = await asyncio.to_thread(get_collection_stats, repo_key, temporary=temporary, namespace=namespace)
    if stats.get("is_stale"):
        days = stats.get("days_old", 30)
        return f"Index is {days} days old. We recommend running 'ensure_repo_ready' to refresh patterns."
    return None


def _contextual_query(intent: str, code: str, max_code_chars: int = 4_000) -> str:
    """Combine a task intent with a bounded code excerpt for semantic retrieval."""
    excerpt = code.strip()[:max_code_chars]
    return f"{intent}\n\nCurrent code excerpt:\n{excerpt}" if excerpt else intent


def register_analysis_tools(mcp):
    @mcp.tool(name="semantic_search_reviews")
    async def semantic_search_reviews(
        query: str,
        repo: str | None = None,
        n_results: int = 15,
        only_ci: bool = False,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Search past review comments semantically.
        Use 'only_ci=True' to filter for PRs that touched CI/CD files (Workflows, Docker, etc).
        Returns raw context snippets for the IDE agent to analyze.
        """
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "semantic_search_reviews")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)

        where = {"touches_ci": True} if only_ci else None

        results = await asyncio.to_thread(
            query_similar,
            repo_key,
            query,
            n_results=n_results,
            temporary=temporary,
            namespace=namespace,
            where=where,
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {"results": results}
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="find_similar_errors")
    async def find_similar_errors(
        error_message: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Search for past PRs where reviewers discussed a similar error message or stack trace.
        High value for engineering managers diagnosing recurring production or CI failures.
        """
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "find_similar_errors")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)

        results = await asyncio.to_thread(
            query_similar,
            repo_key,
            error_message,
            n_results=10,
            temporary=temporary,
            namespace=namespace,
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "error_query": error_message,
            "similar_historical_discussions": results,
            "instruction": "Analyze these historical discussions to see if this error has a known root cause or previously accepted fix."
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="review_code_with_history")
    async def review_code_with_history(
        code: str,
        repo: str | None = None,
        only_ci: bool = False,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical review context relevant to a code snippet.
        The IDE agent should use these snippets to perform a grounded code review.
        Use 'only_ci=True' to focus on PRs that touched CI/CD files.
        """
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "review_code_with_history")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)

        where = {"touches_ci": True} if only_ci else None

        context = await asyncio.to_thread(
            query_similar,
            repo_key,
            code,
            n_results=12,
            temporary=temporary,
            namespace=namespace,
            where=where,
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "task": "code_review",
            "historical_context": context,
            "instruction": "Analyze the provided code using these historical review patterns. Highlight any deviations from team standards."
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="get_team_review_patterns")
    async def get_team_review_patterns(
        topic: str = "general code quality architecture",
        repo: str | None = None,
        only_ci: bool = False,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve recurring historical review patterns for a repository.
        The IDE agent should summarize these into actionable team standards.
        Use 'only_ci=True' to focus on historical CI/CD standards.
        """
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "get_team_review_patterns")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)

        where = {"touches_ci": True} if only_ci else None

        context = await asyncio.to_thread(
            query_similar,
            repo_key,
            topic,
            n_results=25,
            temporary=temporary,
            namespace=namespace,
            where=where,
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "topic": topic,
            "patterns": context,
            "instruction": "Summarize these historical comments into a list of recurring review patterns and team standards."
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="generate_tests")
    async def generate_tests(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical testing patterns relevant to the provided code.
        The IDE agent should generate tests that match this repository's style.
        """
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "generate_tests")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)
        
        context = await asyncio.to_thread(
            query_similar,
            repo_key, 
            _contextual_query("unit testing integration mock fixtures assert", code),
            n_results=12, 
            temporary=temporary, 
            namespace=namespace
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "testing_history": context,
            "instruction": "Generate unit tests for the provided code, following the patterns found in the testing history."
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="static_analysis")
    async def static_analysis(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical linting and style feedback for a repository.
        The IDE agent should perform a manual 'human-like' check based on these patterns.
        """
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "static_analysis")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)
        
        context = await asyncio.to_thread(
            query_similar,
            repo_key, 
            _contextual_query("lint style nit readability clean code formatting", code),
            n_results=12, 
            temporary=temporary, 
            namespace=namespace
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "style_history": context,
            "instruction": "Analyze the code for style and readability issues that match the types of feedback your team has given in the past."
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="suggest_refactors")
    async def suggest_refactors_tool(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical refactoring feedback from the repository.
        The IDE agent should suggest improvements grounded in these patterns.
        """
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "suggest_refactors")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)
        
        context = await asyncio.to_thread(
            query_similar,
            repo_key, 
            _contextual_query("refactor DRY modularity performance complexity abstraction", code),
            n_results=12, 
            temporary=temporary, 
            namespace=namespace
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "refactor_history": context,
            "instruction": "Suggest refactoring improvements for the provided code based on how the team has handled similar complexity in the past."
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)

    @mcp.tool(name="security_check")
    async def security_check(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical security feedback and vulnerability discussions.
        The IDE agent should audit the code for similar risks.
        """
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "security_check")
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)
        
        context = await asyncio.to_thread(
            query_similar,
            repo_key, 
            _contextual_query("security vulnerability injection sanitization auth encryption", code),
            n_results=15, 
            temporary=temporary, 
            namespace=namespace
        )
        
        warning = await _get_stale_warning(repo_key, temporary, namespace)
        response = {
            "repo": repo_key,
            "security_history": context,
            "instruction": "Audit the provided code for security vulnerabilities, paying close attention to patterns flagged in previous reviews."
        }
        if warning:
            response["warning"] = warning
            
        return json.dumps(response, indent=2)
