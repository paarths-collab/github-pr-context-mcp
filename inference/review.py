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
    context_text = "\n\n---\n".join([
        f"[Past review | similarity: {c['similarity']}]\n{c['text']}"
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
    context_text = "\n\n".join([c["text"] for c in retrieved_context])

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
