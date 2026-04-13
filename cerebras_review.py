import os
from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv

load_dotenv()

client = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))

REVIEW_SYSTEM_PROMPT = """You are a senior software engineer doing code review.
You have access to historical PR review comments from this repository.
Use the provided context to give reviews that match the team's standards and catch issues
they've flagged before. Be specific, reference line numbers when possible, be concise.
Do not be sycophantic. Flag real problems."""

def review_with_context(
    diff_or_code: str,
    retrieved_context: list[dict],
    repo: str,
) -> str:
    """Use retrieved RAG context + Cerebras to do a context-aware code review."""
    context_text = "\n\n---\n".join([
        f"[Past review | similarity: {c['similarity']}]\n{c['text']}"
        for c in retrieved_context[:6]  # Top 6 chunks to stay within context
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

    response = client.chat.completions.create(
        model="llama3.1-8b",
        max_tokens=1024,
        messages=[
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )

    return response.choices[0].message.content

def summarize_patterns(retrieved_context: list[dict], repo: str) -> str:
    """Summarize what this team commonly flags in reviews."""
    context_text = "\n\n".join([c["text"] for c in retrieved_context])

    response = client.chat.completions.create(
        model="llama3.1-8b",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Repository: {repo}\n\n"
                f"Here are past code review comments from this team:\n{context_text}\n\n"
                "List the top 5 patterns this team commonly flags in code reviews. "
                "Be specific. Quote examples where useful."
            ),
        }],
    )

    return response.choices[0].message.content
