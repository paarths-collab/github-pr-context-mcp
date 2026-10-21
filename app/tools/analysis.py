import asyncio
import json
from mcp.server.fastmcp import Context
from app.retrieval import (
    DEFAULT_BUDGET,
    GUIDANCE_SUFFIX,
    evidence_response,
    fetch_evidence,
)
from app.state import (
    get_state,
    is_temporary,
    resolve_namespace,
    resolve_repo,
    track_usage,
)


def _contextual_query(intent: str, code: str, max_code_chars: int = 4_000) -> str:
    """Combine a task intent with a bounded code excerpt for semantic retrieval."""
    excerpt = code.strip()[:max_code_chars]
    return f"{intent}\n\nCurrent code excerpt:\n{excerpt}" if excerpt else intent


def register_analysis_tools(mcp):
    async def _scope(ctx, repo, namespace, file_path, tool_name):
        """Resolve namespace, repository and storage for a tool call."""
        if ctx is None:
            raise ValueError("Context is required")
        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, tool_name)
        repo_key = await asyncio.to_thread(resolve_repo, repo, state, file_path=file_path)
        temporary = is_temporary(repo_key, namespace, state)
        return repo_key, namespace, temporary

    @mcp.tool(name="semantic_search_reviews")
    async def semantic_search_reviews(
        query: str,
        repo: str | None = None,
        n_results: int = 15,
        only_ci: bool = False,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        exclude_bots: bool = False,
        source_kinds: list[str] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Search past review discussions (hybrid semantic + exact-identifier match).
        Use 'only_ci=True' to filter for PRs that touched CI/CD files (Workflows, Docker, etc).
        'exclude_bots' drops bot-authored evidence before ranking; 'source_kinds' limits the kinds
        (human_review_thread, bot_review_thread, human_review_summary, bot_review_summary,
        pr_description, commit_message). 'budget_tokens' (256-16000) caps the response size; whole
        evidence units are returned, never truncated.
        Returns an evidence envelope (results, coverage, warnings) for the IDE agent to analyze.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "semantic_search_reviews")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=query,
            n_results=n_results,
            budget_tokens=budget_tokens,
            exclude_bots=exclude_bots,
            source_kinds=source_kinds,
            where={"touches_ci": True} if only_ci else None,
        )

    @mcp.tool(name="find_similar_errors")
    async def find_similar_errors(
        error_message: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        ctx: Context | None = None,
    ) -> str:
        """
        Search for past PRs where reviewers discussed a similar error message or stack trace.
        High value for engineering managers diagnosing recurring production or CI failures.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "find_similar_errors")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=error_message,
            n_results=10,
            budget_tokens=budget_tokens,
            legacy_key="similar_historical_discussions",
            extra={
                "error_query": error_message,
                "instruction": (
                    "Analyze these historical discussions to see if this error has a known root cause "
                    "or previously accepted fix." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="review_code_with_history")
    async def review_code_with_history(
        code: str,
        repo: str | None = None,
        only_ci: bool = False,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        exclude_bots: bool = False,
        source_kinds: list[str] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical review context relevant to a code snippet.
        The IDE agent should use these snippets to perform a grounded code review.
        Use 'only_ci=True' to focus on PRs that touched CI/CD files.
        'exclude_bots', 'source_kinds' and 'budget_tokens' behave as in semantic_search_reviews.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "review_code_with_history")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=code,
            n_results=12,
            budget_tokens=budget_tokens,
            exclude_bots=exclude_bots,
            source_kinds=source_kinds,
            where={"touches_ci": True} if only_ci else None,
            legacy_key="historical_context",
            extra={
                "repo": repo_key,
                "task": "code_review",
                "instruction": (
                    "Analyze the provided code using these historical review patterns. "
                    "Highlight any deviations from team standards." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="get_team_review_patterns")
    async def get_team_review_patterns(
        topic: str = "general code quality architecture",
        repo: str | None = None,
        only_ci: bool = False,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        exclude_bots: bool = False,
        source_kinds: list[str] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve recurring historical review patterns for a repository.
        The IDE agent should summarize these into actionable team standards.
        Use 'only_ci=True' to focus on historical CI/CD standards.
        'exclude_bots', 'source_kinds' and 'budget_tokens' behave as in semantic_search_reviews.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "get_team_review_patterns")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=topic,
            n_results=25,
            budget_tokens=budget_tokens,
            exclude_bots=exclude_bots,
            source_kinds=source_kinds,
            where={"touches_ci": True} if only_ci else None,
            legacy_key="patterns",
            extra={
                "repo": repo_key,
                "topic": topic,
                "instruction": (
                    "Summarize these historical comments into a list of recurring review patterns "
                    "and team standards." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="generate_tests")
    async def generate_tests(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical testing patterns relevant to the provided code.
        The IDE agent should generate tests that match this repository's style.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "generate_tests")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=_contextual_query("unit testing integration mock fixtures assert", code),
            n_results=12,
            budget_tokens=budget_tokens,
            legacy_key="testing_history",
            extra={
                "repo": repo_key,
                "instruction": (
                    "Generate unit tests for the provided code, following the patterns found in the "
                    "testing history." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="static_analysis")
    async def static_analysis(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical linting and style feedback for a repository.
        The IDE agent should perform a manual 'human-like' check based on these patterns.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "static_analysis")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=_contextual_query("lint style nit readability clean code formatting", code),
            n_results=12,
            budget_tokens=budget_tokens,
            legacy_key="style_history",
            extra={
                "repo": repo_key,
                "instruction": (
                    "Analyze the code for style and readability issues that match the types of feedback "
                    "your team has given in the past." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="suggest_refactors")
    async def suggest_refactors_tool(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical refactoring feedback from the repository.
        The IDE agent should suggest improvements grounded in these patterns.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "suggest_refactors")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=_contextual_query("refactor DRY modularity performance complexity abstraction", code),
            n_results=12,
            budget_tokens=budget_tokens,
            legacy_key="refactor_history",
            extra={
                "repo": repo_key,
                "instruction": (
                    "Suggest refactoring improvements for the provided code based on how the team has "
                    "handled similar complexity in the past." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="security_check")
    async def security_check(
        code: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET,
        ctx: Context | None = None,
    ) -> str:
        """
        Retrieve historical security feedback and vulnerability discussions.
        The IDE agent should audit the code for similar risks.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "security_check")
        return await evidence_response(
            repo_key=repo_key,
            namespace=namespace,
            temporary=temporary,
            query=_contextual_query("security vulnerability injection sanitization auth encryption", code),
            n_results=15,
            budget_tokens=budget_tokens,
            legacy_key="security_history",
            extra={
                "repo": repo_key,
                "instruction": (
                    "Audit the provided code for security vulnerabilities, paying close attention to "
                    "patterns flagged in previous reviews." + GUIDANCE_SUFFIX
                ),
            },
        )

    @mcp.tool(name="get_evidence")
    async def get_evidence(
        evidence_id: str,
        repo: str | None = None,
        namespace: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """
        Expand one evidence unit (by the evidence_id from any search result) to every part of its
        review thread or message, returned in the standard evidence envelope. Use it when a search
        result's part_count is greater than 1 or its excerpt looks cut off.
        """
        repo_key, namespace, temporary = await _scope(ctx, repo, namespace, file_path, "get_evidence")
        envelope = await asyncio.to_thread(fetch_evidence, repo_key, namespace, temporary, evidence_id)
        return json.dumps(envelope, indent=2)
