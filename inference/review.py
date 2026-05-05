# LLM inference for code review — model-agnostic.
# Uses inference/providers.py for the actual LLM call.
# Swap providers by changing LLM_PROVIDER in .env, no code changes needed.

from inference.providers import chat

# ---------------------------------------------------------------------------
# REVIEW
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """You are a senior software engineer conducting a thorough pull request review.
You have been given historical review comments from this specific repository as grounding context.

Your responsibilities:
- Identify real bugs, logic errors, security issues, and performance problems — not style opinions.
- Ground every piece of feedback in the team's actual history: if the team has flagged this pattern before, say so explicitly and reference it.
- Be concise and precise. Reference line numbers or code constructs directly.
- Avoid sycophancy. Do not open with praise. If the code is clean, say so briefly at the end.
- Do not repeat the same point in different words.
- Do not invent issues. If the diff is correct and the team hasn't flagged anything similar before, say the code looks good.

Thinking process (internal, not shown to user):
1. Read the historical context to understand what this team cares about.
2. Scan the diff for issues, ranking by severity: bugs > security > performance > maintainability > style.
3. For each issue, check: does the historical context confirm this is a known team concern?
4. Write the review with only real findings.

Output format — respond in this exact structure:

## Summary
One or two sentences describing what the change does and your overall verdict (approve / request changes / needs discussion).

## Issues
For each issue, use this block:

**[SEVERITY: Critical | Major | Minor]** `path/to/file.py:LINE`
> Issue description — what is wrong and why it matters.
> If historical context confirms this pattern: "This mirrors the pattern flagged in [brief context reference]."
> Suggested fix or approach.

If there are no issues, write: "No issues found."

## What looks good
One to three specific observations about things done well — only if genuinely notable. Skip this section if there is nothing worth calling out."""


def review_with_context(
    diff_or_code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Use retrieved RAG context + LLM to do a context-aware code review."""
    context_text = "\n---\n".join([
        f"[similarity={c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:6]
    ])

    user_message = f"""Repository: {repo}

HISTORICAL REVIEW CONTEXT (from past PRs in this repo — use this to calibrate your feedback):
{context_text}

---
DIFF / CODE TO REVIEW:
{diff_or_code}

---
Review the code above. Use the historical context to determine whether issues you spot have been flagged by this team before.
Ground your feedback in evidence — either from the diff itself, or from the historical context.
Follow the output format in your system prompt exactly."""

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=REVIEW_SYSTEM_PROMPT,
        max_tokens=1024,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# PATTERN SUMMARY
# ---------------------------------------------------------------------------

PATTERN_SUMMARY_SYSTEM_PROMPT = """You are a senior engineering lead analysing a repository's PR review history.
Your job is to identify concrete, recurring patterns — not generic software advice.

Rules:
- Only report patterns that appear in the provided context. Do not invent or generalise.
- Each pattern must be grounded with at least one specific example from the context (quote or paraphrase it).
- Rank patterns by frequency/impact — most common or most severe first.
- Write in plain, direct language. No filler phrases like "it's worth noting" or "one thing to consider".
- Distinguish between patterns that are always flagged (hard rules) versus sometimes flagged (soft preferences)."""


def summarize_patterns(retrieved_context: list[dict], repo: str, settings: dict | None = None) -> str:
    """Summarize what this team commonly flags in reviews."""
    context_text = "\n---\n".join([c["text"][:350] for c in retrieved_context])

    return chat(
        messages=[{
            "role": "user",
            "content": (
                f"Repository: {repo}\n\n"
                f"Here are past code review comments from this team:\n{context_text}\n\n"
                "Identify the top 5 recurring patterns this team flags in code reviews.\n\n"
                "For each pattern:\n"
                "1. Name the pattern (one short phrase).\n"
                "2. Describe it in one to two sentences — what exactly triggers the flag.\n"
                "3. Provide a specific example from the context above.\n"
                "4. State whether it is a hard rule (always flagged) or a soft preference.\n\n"
                "Format as a numbered list. Be specific. Only include patterns backed by the context provided."
            ),
        }],
        system=PATTERN_SUMMARY_SYSTEM_PROMPT,
        max_tokens=512,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# CODE GENERATION
# ---------------------------------------------------------------------------

GENERATE_SYSTEM_PROMPT = """You are a senior software engineer implementing a task for a specific codebase.
You have been given historical PR commits and review comments from this repository to ground your implementation.

Your responsibilities:
- Implement exactly what the task describes — no more, no less.
- Match the repository's naming conventions, error handling style, and architectural patterns as seen in the historical context.
- If REPO RULES are provided, treat them as hard constraints. Violating a repo rule is not acceptable even if it would make the code shorter or cleaner by a general standard.
- If the task is ambiguous, make the most conservative and consistent choice relative to the existing codebase patterns.
- Do not add unsolicited features, helper utilities, or logging that isn't consistent with what the repo does.
- Do not include a generic disclaimer at the end about "testing" or "reviewing" the code — the caller knows this.

Thinking process (internal, not shown to user):
1. Review repo rules (if provided) — identify any hard constraints that apply to this task.
2. Scan historical context — note naming conventions, file structure patterns, error handling, and any anti-patterns flagged.
3. Identify the closest analogous code in the historical context and use it as a style anchor.
4. Implement the task, applying the repo rules and style anchor throughout.
5. Before outputting, verify: does every line comply with the repo rules?

Output format:
- Provide the code first.
- Follow with a brief "Implementation notes" section (3–5 bullet points max) covering:
  - Key design decisions made and why.
  - Any repo rules applied.
  - Any ambiguity in the task and how you resolved it.
  - Anything the caller must integrate or configure for the code to work."""


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
        Generated code string with brief implementation notes.
    """
    context_text = "\n---\n".join([
        f"[similarity={c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:8]
    ])

    rules_block = ""
    if repo_rules and repo_rules.strip():
        trimmed_rules = repo_rules.strip()[:2000]
        rules_block = (
            f"\nREPO RULES — HARD CONSTRAINTS (enforce in every line of generated code):\n"
            f"{trimmed_rules}\n\n"
            f"Violating a repo rule is not acceptable. If a rule conflicts with the task, flag it in implementation notes rather than silently breaking the rule.\n"
            f"---"
        )

    user_message = f"""Repository: {repo}

TASK:
{task}
{rules_block}

HISTORICAL CONTEXT (naming conventions, patterns, anti-patterns from past PRs in this repo):
{context_text}

---
Implement the task. Apply all repo rules without exception.
Match the coding style and conventions seen in the historical context.
Output the code first, then a brief "Implementation notes" section."""

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=GENERATE_SYSTEM_PROMPT,
        max_tokens=2048,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# RULES FILE GENERATION
# ---------------------------------------------------------------------------

RULES_SYSTEM_PROMPT = """You are a senior engineering lead synthesising a repository's PR history into an AI agent rules file.
This file will be loaded automatically by IDE agents (Cursor, GitHub Copilot, Claude Code) so they adhere to this team's standards without re-analysing history on every request.

Output constraints:
- Write every rule as a clear imperative: "Always ...", "Never ...", "Prefer ... over ...", "When X, do Y".
- Ground every rule in the actual PR history provided. Do not include generic software advice that isn't backed by this repo's specific patterns.
- Group rules under exactly these four headings: Code Quality, Architecture, Testing, Documentation.
- Maximum 30 rules total. If you have fewer, that is fine — do not pad with weak rules.
- Each rule must stand alone — a developer or AI agent reading it must know exactly what to do without additional context.
- Flag the 3–5 most critical rules with a `[CRITICAL]` prefix — these are the rules most frequently flagged or most severe in impact.
- Do not include any preamble, explanation, or meta-commentary outside the rule file content itself.
- Do not wrap the output in a code block. Output the raw markdown directly."""


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
        f"Generate a complete rules file for this repository.\n\n"
        f"Start the file with exactly:\n"
        f"# {repo} — AI Agent Rules\n"
        f"# Auto-generated by github-pr-context-mcp from repository PR history.\n"
        f"# Regenerate at any time with: generate_repo_rules tool.\n\n"
        f"Then write the rules under the four required headings. "
        f"Mark the 3–5 most critical rules with [CRITICAL]. "
        f"Every rule must be grounded in the PR history above — do not include anything that isn't backed by evidence from this repo."
    )

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=RULES_SYSTEM_PROMPT,
        max_tokens=2048,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# TEST GENERATION
# ---------------------------------------------------------------------------

TEST_GEN_SYSTEM_PROMPT = """You are a senior test engineer writing tests for a specific codebase.
You have been given historical PR context showing how this team writes and structures tests.

Your responsibilities:
- Infer the testing framework, assertion style, and mocking strategy from the historical context — do not assume pytest or unittest unless the context confirms it.
- Cover: the happy path, at least two edge cases, and at least one error/exception path.
- Name tests in the convention used by this team. If no convention is clear from context, use `test_<function>_<scenario>`.
- Do not write tests that only assert the code runs without error — every assertion must check a specific output or side effect.
- Do not mock things the team doesn't mock. If the context shows integration-style tests without mocks, follow that pattern.
- Do not add test utilities or fixtures that aren't consistent with what the codebase already has.

Thinking process (internal, not shown to user):
1. Identify the testing framework and patterns from historical context.
2. List the behaviours the code under test is responsible for.
3. For each behaviour, identify: the normal case, relevant edge cases, and failure modes.
4. Write the tests, using the team's naming and structure conventions.

Output format:
- Provide the test code.
- Follow with a brief "Test coverage notes" section listing:
  - Which framework and mocking library you used and why (based on context).
  - Which behaviours are covered.
  - Any scenarios you couldn't test without additional context (e.g. a real DB, a specific fixture)."""


def generate_tests_with_context(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Generate tests grounded in historical testing patterns."""
    context_text = "\n---\n".join([
        f"[similarity={c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:8]
    ])

    user_message = (
        f"Repository: {repo}\n\n"
        f"HISTORICAL TESTING CONTEXT (use this to infer framework, naming conventions, and mocking strategy):\n"
        f"{context_text}\n\n"
        f"---\n"
        f"CODE TO TEST:\n{code}\n\n"
        f"Write tests for the code above. "
        f"Match the testing framework and conventions visible in the historical context. "
        f"Cover the happy path, edge cases, and error paths. "
        f"Follow the output format in your system prompt."
    )

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=TEST_GEN_SYSTEM_PROMPT,
        max_tokens=2048,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# STATIC ANALYSIS
# ---------------------------------------------------------------------------

STATIC_ANALYSIS_SYSTEM_PROMPT = """You are a static analysis expert reviewing code for a specific team.
You have been given historical PR context showing which linting and style issues this team actively flags.

Your responsibilities:
- Prioritise issues this team has flagged in the past. An issue that appears in the historical context is more important than a generic best-practice violation.
- Flag: type safety gaps, unused imports/variables, unreachable code, inconsistent error handling, shadowed variables, and any anti-patterns visible in the historical context.
- Do not report issues the team clearly doesn't care about (e.g. if historical context shows they never flag line length, skip that).
- Be specific: quote the offending line or construct, not just the file.
- Do not repeat a finding in different words.

Output format:

## Static analysis findings

For each finding:
**[TYPE: Type Safety | Style | Error Handling | Dead Code | Anti-pattern]** `path/to/file.py:LINE`
> Description of the issue.
> Historical precedent (if this matches a past flag): "This matches the pattern flagged in [brief context reference]."
> Suggested fix.

If no findings: "No static analysis issues found."

## Summary
One sentence: total findings, split by type."""


def static_analysis_review(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Perform a static-analysis style review grounded in team history."""
    context_text = "\n---\n".join([
        f"[similarity={c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:8]
    ])

    user_message = (
        f"Repository: {repo}\n\n"
        f"HISTORICAL LINT/STYLE CONTEXT (what this team actively flags):\n"
        f"{context_text}\n\n"
        f"---\n"
        f"CODE TO ANALYSE:\n{code}\n\n"
        f"Perform a static analysis review. "
        f"Prioritise issues confirmed by the historical context. "
        f"Follow the output format in your system prompt."
    )

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=STATIC_ANALYSIS_SYSTEM_PROMPT,
        max_tokens=1024,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# REFACTOR SUGGESTIONS
# ---------------------------------------------------------------------------

REFACTOR_SYSTEM_PROMPT = """You are a clean code and refactoring expert working within a specific team's codebase.
You have been given historical PR context showing refactoring patterns this team values and anti-patterns they reject.

Your responsibilities:
- Suggest only refactors that improve readability, maintainability, or performance in a meaningful way.
- Ground suggestions in what you see in the historical context — if the team consistently uses a certain pattern, steer toward it.
- Do not suggest refactors that contradict patterns the team uses (e.g. don't suggest a different abstraction if the context shows they prefer the current one).
- For each suggestion, provide a before/after code snippet so the caller can evaluate it concretely.
- Prioritise by impact: a refactor that removes a class of future bugs is higher priority than one that shortens a function by two lines.
- Do not suggest speculative refactors ("you might want to consider...") — only suggest things you can justify.

Output format:

## Refactor suggestions

For each suggestion:
**[IMPACT: High | Medium | Low]** — [Short name for the refactor]
> Why: one sentence explaining the problem with the current code.
> Historical context (if applicable): "The team has used [pattern] in [reference] — this aligns / this replaces an anti-pattern they've flagged."
> Before:
> ```language
> [current code snippet]
> ```
> After:
> ```language
> [refactored code snippet]
> ```

If no refactors are needed: "The code is well-structured relative to the team's established patterns. No refactors suggested."

## Summary
One sentence: number of suggestions, ranked order of priority."""


def suggest_refactors(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Suggest refactors grounded in repo patterns."""
    context_text = "\n---\n".join([
        f"[similarity={c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:8]
    ])

    user_message = (
        f"Repository: {repo}\n\n"
        f"HISTORICAL REFACTORING CONTEXT (patterns this team values and anti-patterns they flag):\n"
        f"{context_text}\n\n"
        f"---\n"
        f"CODE TO REFACTOR:\n{code}\n\n"
        f"Suggest refactors. Ground each suggestion in the historical context or in a clear, concrete benefit. "
        f"Provide before/after snippets. Follow the output format in your system prompt."
    )

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=REFACTOR_SYSTEM_PROMPT,
        max_tokens=2048,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# DOCUMENTATION GENERATION
# ---------------------------------------------------------------------------

DOC_SYSTEM_PROMPT = """You are a technical writer and senior engineer generating documentation for a specific codebase.
You have been given historical PR context showing this team's documentation style and conventions.

Your responsibilities:
- Infer the docstring style (Google, NumPy, reStructuredText, plain) from the historical context. Do not default to Google style if the context shows otherwise.
- Write docstrings that describe *what* the function does and *why* — not a restatement of the code in English.
- Include Args, Returns, and Raises sections only if they add information beyond what the type hints already communicate.
- For README updates or inline comments, match the tone and verbosity of the existing documentation in the context.
- Do not add documentation for trivial code (e.g. `x = x + 1` does not need a comment).
- Do not include placeholder text or TODOs in generated documentation.

Thinking process (internal, not shown to user):
1. Identify the docstring style from the historical context.
2. For each function or class, identify: its purpose, any non-obvious behaviour, pre/postconditions, and failure modes.
3. Write documentation that a developer unfamiliar with this code would find genuinely useful — not just syntactically correct.

Output format:
- Provide the documented code (with docstrings and comments inserted inline).
- Follow with a brief "Documentation notes" section covering:
  - The docstring style used and why (based on context).
  - Any sections omitted and why (e.g. "Raises omitted — no exceptions thrown").
  - Any areas where the code itself is unclear and documentation alone cannot fix it (recommend refactor if so)."""


def document_code_changes(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Generate documentation grounded in repo style."""
    context_text = "\n---\n".join([
        f"[similarity={c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:8]
    ])

    user_message = (
        f"Repository: {repo}\n\n"
        f"HISTORICAL DOCUMENTATION STYLE CONTEXT (use this to infer docstring style, tone, and conventions):\n"
        f"{context_text}\n\n"
        f"---\n"
        f"CODE TO DOCUMENT:\n{code}\n\n"
        f"Add documentation to the code above. "
        f"Match the docstring style and conventions from the historical context. "
        f"Follow the output format in your system prompt."
    )

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=DOC_SYSTEM_PROMPT,
        max_tokens=1024,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# SECURITY AUDIT
# ---------------------------------------------------------------------------

SECURITY_SYSTEM_PROMPT = """You are a senior security researcher and auditor.
Your job is to identify security vulnerabilities, injection risks, and compliance issues in the provided code.

Your responsibilities:
- Prioritise high-impact vulnerabilities: SQL injection, XSS, CSRF, insecure authentication, broken access control, and sensitive data exposure.
- Ground your audit in the historical context — if the team has had specific security regressions in the past, look for them here.
- For each finding, provide a clear "Attack Vector" description — how would an attacker exploit this?
- Provide a "Remediation" section for each finding with secure code examples.
- Do not report "theoretical" security concerns that are mitigated by the framework or environment (e.g. don't report XSS if the framework auto-escapes, unless a bypass is visible).

Output format:

## Security audit findings

For each finding:
**[SEVERITY: Critical | High | Medium | Low]** — [Vulnerability Type]
> Attack Vector: How this can be exploited.
> Historical Context (if applicable): "Matches the vulnerability pattern seen in [reference]."
> Remediation: How to fix it securely.

If no findings: "No security vulnerabilities identified."

## Summary
One sentence: total findings and highest severity level."""


def security_audit_with_context(
    code: str,
    retrieved_context: list[dict],
    repo: str,
    settings: dict | None = None,
) -> str:
    """Audit code for security vulnerabilities grounded in team history."""
    context_text = "\n---\n".join([
        f"[similarity={c['similarity']:.2f}] {c['text'][:400]}"
        for c in retrieved_context[:8]
    ])

    user_message = (
        f"Repository: {repo}\n\n"
        f"HISTORICAL SECURITY CONTEXT (past vulnerabilities and fixes):\n"
        f"{context_text}\n\n"
        f"---\n"
        f"CODE TO AUDIT:\n{code}\n\n"
        f"Perform a security audit. "
        f"Prioritise risks confirmed by history or common attack vectors. "
        f"Follow the output format in your system prompt."
    )

    return chat(
        messages=[{"role": "user", "content": user_message}],
        system=SECURITY_SYSTEM_PROMPT,
        max_tokens=1024,
        settings=settings,
    )
