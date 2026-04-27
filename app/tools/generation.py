import pathlib
from mcp.server.fastmcp import Context
from inference import generate_with_context, generate_rules_content
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

def register_generation_tools(mcp):
    @mcp.tool(name="generate_code_from_history")
    def generate_code_from_history(
        task: str,
        repo: str | None = None,
        namespace: str | None = None,
        rules_file: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Generate code grounded in historical PR patterns and review feedback."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "generate_code_from_history")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)

        # --- Auto-load repo rules file ---
        repo_rules: str | None = None
        rules_source: str | None = None

        if rules_file:
            candidate = pathlib.Path(rules_file)
            if candidate.exists():
                repo_rules = candidate.read_text(encoding="utf-8", errors="replace")
                rules_source = str(candidate)
        else:
            for candidate_name in (".cursorrules", "CLAUDE.md", ".github/copilot-instructions.md"):
                candidate = pathlib.Path(candidate_name)
                if candidate.exists():
                    repo_rules = candidate.read_text(encoding="utf-8", errors="replace")
                    rules_source = str(candidate)
                    break

        user_settings = current_user_settings()
        context = query_similar(
            repo_key,
            task,
            n_results=12,
            temporary=temporary,
            namespace=namespace,
        )
        result = generate_with_context(
            task, context, repo_key,
            settings=llm_settings(user_settings),
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

    @mcp.tool(name="generate_repo_rules")
    def generate_repo_rules(
        output_path: str = ".cursorrules",
        repo: str | None = None,
        namespace: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Generate a rules file (.cursorrules/CLAUDE.md) grounded in this repo's PR history."""
        if ctx is None:
            raise ValueError("Context is required")

        state = get_state(ctx)
        namespace = resolve_namespace(namespace, state)
        track_usage(ctx, namespace, "generate_repo_rules")
        repo_key = resolve_repo(repo, state)
        temporary = is_temporary(repo_key, namespace, state)

        user_settings = current_user_settings()
        context = query_similar(
            repo_key,
            "code quality architecture testing documentation style conventions",
            n_results=25,
            temporary=temporary,
            namespace=namespace,
        )

        rules_content = generate_rules_content(context, repo_key, settings=llm_settings(user_settings))

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
            return f"Could not write to '{output_path}': {e}\n\nGenerated content:\n\n{rules_content}"
