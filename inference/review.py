# LLM inference for code review — model-agnostic.
# Uses inference/providers.py for the actual LLM call.
# Swap providers by changing LLM_PROVIDER in .env, no code changes needed.

from inference.providers import chat

REVIEW_SYSTEM_PROMPT = """You are a senior software engineer doing code review.
You have access to historical PR review comments from this repository.
Use the provided context to give reviews that match the team's standards and catch issues
they've flagged before. Be specific, reference line numbers when possible, be concise.
Do not be sycophantic. Flag real problems."""


def review_with_context(
    diff_or_code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Use retrieved RAG context + LLM to do a context-aware code review."""
    context_text = "\n---\n".join([
        f"[{c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:6]
    ])

    user_message = f"""Repository: {repo}

HISTORICAL REVIEW CONTEXT (from past PRs in this repo):
{context_text}

---
CODE TO REVIEW:
{diff_or_code}

---
Provide a thorough code review. Reference specific past patterns where relevant.
Flag issues the team has flagged before. Note what looks good too."""

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=REVIEW_SYSTEM_PROMPT,
        max_tokens=1024,
        settings=settings,
    )


def summarize_patterns(retrieved_context: list[dict], repo: str, settings: dict | None = None) -> str:
    """Summarize what this team commonly flags in reviews."""
    context_text = "\n---\n".join([c["text"][:350] for c in retrieved_context])

    return chat(
        messages=[{
            "role": "user",
            "content": (
                f"Repository: {repo}\n\n"
                f"Here are past code review comments from this team:\n{context_text}\n\n"
                "List the top 5 patterns this team commonly flags in code reviews. "
                "Be specific. Quote examples where useful."
            ),
        }],
        max_tokens=512,
        settings=settings,
    )


GENERATE_SYSTEM_PROMPT = """You are a senior software engineer assistant.
You write code that follows the repository's established patterns, naming conventions, and best practices.
You have access to historical PR commits and review comments from this repository.
Use the provided context to ensure your generated code matches the team's style and avoids issues they've flagged in the past."""


def generate_with_context(
    task: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
    repo_rules: str | None = None,
) -> str:
    """Use retrieved RAG context + LLM to generate code grounded in team patterns.

    Args:
        task: Description of what to implement.
        retrieved_context: RAG documents from the indexed repo.
        repo: GitHub repo identifier (owner/name).
        settings: Optional LLM provider override dict.
        repo_rules: Contents of a .cursorrules / CLAUDE.md file. When provided,
                    these rules are injected as hard constraints before historical context.

    Returns:
        Generated code string.
    """
    context_text = "\n---\n".join([
        f"[{c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:8]
    ])

    rules_block = ""
    if repo_rules and repo_rules.strip():
        # Truncate to 2000 chars — rules files are dense; first 2000 chars cover all hard rules
        trimmed_rules = repo_rules.strip()[:2000]
        rules_block = f"\nREPO RULES (enforce in ALL generated code):\n{trimmed_rules}\n\n---"

    user_message = f"""Repository: {repo}

TASK:
{task}
{rules_block}

HISTORICAL CONTEXT (from past PRs in this repo):
{context_text}

---
Write the code to complete the task. You MUST follow all REPO RULES above without exception.
Ensure the output also matches the coding style, naming conventions, and best practices seen in the historical context.
Avoid issues the team has flagged before in similar situations.
Provide only the code and necessary brief explanations."""

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=GENERATE_SYSTEM_PROMPT,
        max_tokens=2048,
        settings=settings,
    )


RULES_SYSTEM_PROMPT = """You are a senior engineering lead.
Your job is to synthesize a repository's historical PR review comments into a concise,
actionable set of rules for IDE agents (Cursor, GitHub Copilot, Claude).

Output format rules:
- Write in clear, imperative statements ("Always ...", "Never ...", "Prefer ...").
- Group rules under the headings: Code Quality, Architecture, Testing, Documentation.
- Maximum 30 rules total. Be specific. Reference concrete examples from the context.
- Do NOT include generic advice not backed by the repo's real history.
- Do NOT include any preamble or explanation outside the rule file content itself."""


def generate_rules_content(
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Synthesise a .cursorrules / CLAUDE.md / copilot-instructions.md file from indexed PR history.

    Args:
        retrieved_context: Retrieved RAG documents from the indexed repo.
        repo: The GitHub repo identifier (owner/name).
        settings: Optional LLM provider override dict.

    Returns:
        A markdown string ready to be written as a rules file.
    """
    context_text = "\n\n".join([c["text"] for c in retrieved_context])

    user_message = (
        f"Repository: {repo}\n\n"
        f"Here are historical PR review comments, commit messages, and code patterns from this repository:\n\n"
        f"{context_text}\n\n"
        f"---\n"
        f"Generate a complete `.cursorrules` / `CLAUDE.md` / `copilot-instructions.md` file "
        f"for this repository. The file will be loaded automatically by IDE agents so they "
        f"adhere to this team's standards without needing to re-analyse the PR history.\n\n"
        f"Start the file with:\n"
        f"# {repo} — AI Agent Rules\n"
        f"# Auto-generated by github-pr-context-mcp from repository PR history.\n"
        f"# Regenerate at any time with: generate_repo_rules tool.\n\n"
        f"Then write the rules."
    )

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=RULES_SYSTEM_PROMPT,
        max_tokens=2048,
        settings=settings,
    )


TEST_GEN_SYSTEM_PROMPT = """You are a senior test engineer.
Your goal is to generate comprehensive unit and integration tests for the provided code.
Use the repository's historical PR context to identify common testing patterns, frameworks (pytest, unittest, etc.), and mocking strategies used by the team.
Ensure your tests cover edge cases, error handling, and follow the team's naming conventions for tests."""


def generate_tests_with_context(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Generate tests grounded in historical testing patterns."""
    context_text = "\n---\n".join([f"[{c['similarity']:.2f}] {c['text'][:400]}" for c in retrieved_context[:8]])
    user_message = f"Repository: {repo}\n\nHISTORICAL TESTING CONTEXT:\n{context_text}\n\n---\nCODE TO TEST:\n{code}\n\nGenerate appropriate tests."
    return chat(messages=[{"role": "user", "content": user_message}], system=TEST_GEN_SYSTEM_PROMPT, max_tokens=2048, settings=settings)


STATIC_ANALYSIS_SYSTEM_PROMPT = """You are a static analysis expert.
Scan the code for issues that a linter or static analyzer would catch, but ground your feedback in what this SPECIFIC team cares about.
Use historical PR context to see what linting or style issues are frequently flagged by humans in this repo."""


def static_analysis_review(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Perform a static-analysis style review grounded in team history."""
    context_text = "\n---\n".join([f"[{c['similarity']:.2f}] {c['text'][:400]}" for c in retrieved_context[:8]])
    user_message = f"Repository: {repo}\n\nHISTORICAL LINT/STYLE CONTEXT:\n{context_text}\n\n---\nCODE TO ANALYZE:\n{code}"
    return chat(messages=[{"role": "user", "content": user_message}], system=STATIC_ANALYSIS_SYSTEM_PROMPT, max_tokens=1024, settings=settings)


REFACTOR_SYSTEM_PROMPT = """You are a clean code and refactoring expert.
Suggest refactorings for the provided code to improve readability, maintainability, and performance.
Base your suggestions on patterns seen in the repository's successful PRs."""


def suggest_refactors(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Suggest refactors grounded in repo patterns."""
    context_text = "\n---\n".join([f"[{c['similarity']:.2f}] {c['text'][:400]}" for c in retrieved_context[:8]])
    user_message = f"Repository: {repo}\n\nHISTORICAL REFACTORING CONTEXT:\n{context_text}\n\n---\nCODE TO REFACTOR:\n{code}"
    return chat(messages=[{"role": "user", "content": user_message}], system=REFACTOR_SYSTEM_PROMPT, max_tokens=2048, settings=settings)


DOC_SYSTEM_PROMPT = """You are a technical writer and senior dev.
Generate documentation (docstrings, README updates, or inline comments) for the code changes.
Use historical context to match the team's documentation style (e.g. Google style, NumPy style)."""


def document_code_changes(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Generate documentation grounded in repo style."""
    context_text = "\n---\n".join([f"[{c['similarity']:.2f}] {c['text'][:400]}" for c in retrieved_context[:8]])
    user_message = f"Repository: {repo}\n\nHISTORICAL DOC STYLE CONTEXT:\n{context_text}\n\n---\nCODE TO DOCUMENT:\n{code}"
    return chat(messages=[{"role": "user", "content": user_message}], system=DOC_SYSTEM_PROMPT, max_tokens=1024, settings=settings)
