import json
from mcp.server.fastmcp import Context
from inference import (
    review_with_context,
    summarize_patterns,
    generate_tests_with_context,
    static_analysis_review,
    suggest_refactors,
    document_code_changes,
    security_audit_with_context
)
from storage import query_similar
from app.state import (
    resolve_namespace,
    get_state,
    current_user_settings,
    track_usage,
    resolve_repo,
    is_temporary,
    llm_settings
)

def register_analysis_tools(mcp):
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

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "semantic_search_reviews")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)

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

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "review_code_with_history")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)

        user_settings = current_user_settings()
        context = query_similar(
            repo_key,
            code,
            n_results=10,
            temporary=temporary,
            namespace=namespace,
        )
        return review_with_context(code, context, repo_key, settings=llm_settings(user_settings))

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

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "get_team_review_patterns")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)

        user_settings = current_user_settings()
        context = query_similar(
            repo_key,
            topic,
            n_results=20,
            temporary=temporary,
            namespace=namespace,
        )
        return summarize_patterns(context, repo_key, settings=llm_settings(user_settings))

    @mcp.tool(name="generate_tests")
    def generate_tests(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Generate unit tests grounded in repository's testing style."""
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "generate_tests")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)
        user_settings = current_user_settings()
        context = query_similar(repo_key, "unit testing integration mock fixtures", n_results=10, temporary=temporary, namespace=namespace)
        return generate_tests_with_context(code, context, repo_key, settings=llm_settings(user_settings))

    @mcp.tool(name="static_analysis")
    def static_analysis(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Perform a human-like static analysis based on historical review feedback."""
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "static_analysis")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)
        user_settings = current_user_settings()
        context = query_similar(repo_key, "lint style nit readability clean code", n_results=10, temporary=temporary, namespace=namespace)
        return static_analysis_review(code, context, repo_key, settings=llm_settings(user_settings))

    @mcp.tool(name="suggest_refactors")
    def suggest_refactors_tool(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Suggest refactorings based on repository's clean code patterns."""
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "suggest_refactors")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)
        user_settings = current_user_settings()
        context = query_similar(repo_key, "refactor DRY modularity performance", n_results=10, temporary=temporary, namespace=namespace)
        return suggest_refactors(code, context, repo_key, settings=llm_settings(user_settings))

    @mcp.tool(name="document_changes")
    def document_changes(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Generate documentation matching the team's style."""
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "document_changes")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)
        user_settings = current_user_settings()
        context = query_similar(repo_key, "docstring comment README documentation", n_results=10, temporary=temporary, namespace=namespace)
        return document_code_changes(code, context, repo_key, settings=llm_settings(user_settings))

    @mcp.tool(name="security_check")
    def security_check(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Check code for security vulnerabilities and compliance issues."""
        if ctx is None: raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "security_check")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)
        user_settings = current_user_settings()
        # Query for past security-related feedback
        context = query_similar(repo_key, "security vulnerability injection sanitization auth", n_results=10, temporary=temporary, namespace=namespace)
        return security_audit_with_context(code, context, repo_key, settings=llm_settings(user_settings))
