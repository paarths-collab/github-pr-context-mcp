"""
Pattern clustering for PR review comments.
Groups raw retrieval results into deduplicated, named themes.
No new dependencies — uses the existing encoder for similarity checks.
"""
from __future__ import annotations
import re
from collections import defaultdict
from storage.vector_store import query_similar

# ── Theme taxonomy ────────────────────────────────────────────────────────────
# Each theme is a (label, keyword_set) pair. A comment is assigned to the FIRST
# theme whose keywords appear in the comment text.  Order matters — more specific
# themes should come first.
_THEMES: list[tuple[str, set[str]]] = [
    ("Error Handling",       {"exception", "error", "raise", "catch", "try", "traceback", "panic", "recover", "unwrap", "result", "fallback"}),
    ("Security",             {"injection", "xss", "csrf", "auth", "token", "secret", "password", "encrypt", "sanitize", "permission", "vulnerability", "sql"}),
    ("Testing",              {"test", "assert", "mock", "fixture", "coverage", "spec", "unit", "integration", "e2e", "stub", "pytest", "jest"}),
    ("Performance",          {"slow", "n+1", "cache", "index", "query", "latency", "memory", "allocat", "loop", "batch", "paginate", "timeout"}),
    ("Code Style",           {"naming", "indent", "format", "lint", "readab", "docstring", "comment", "whitespace", "convention", "nit", "style"}),
    ("Architecture",         {"pattern", "abstraction", "coupling", "cohesion", "solid", "dependency", "layer", "module", "interface", "design", "separat"}),
    ("API Design",           {"endpoint", "route", "parameter", "payload", "schema", "response", "request", "contract", "versioning", "rest", "graphql"}),
    ("Documentation",        {"docs", "docstring", "readme", "comment", "explain", "clarif", "inline", "annotation", "jsdoc", "changelog"}),
    ("CI/CD",                {"pipeline", "workflow", "action", "deploy", "build", "ci", "cd", "docker", "container", "k8s", "kubernetes", "terraform"}),
    ("Type Safety",          {"type", "typed", "annotation", "mypy", "typescript", "generics", "cast", "any", "nullable", "optional", "interface"}),
    ("Database",             {"migration", "schema", "query", "transaction", "index", "orm", "sql", "nosql", "redis", "postgres", "mongo"}),
    ("Concurrency",          {"race", "deadlock", "async", "await", "thread", "lock", "mutex", "concurrent", "parallel", "goroutine", "coroutine"}),
]

_UNCATEGORIZED = "General Feedback"


def _assign_theme(text: str) -> str:
    """Assign a theme label to a piece of text by keyword matching."""
    lower = text.lower()
    for label, keywords in _THEMES:
        if any(kw in lower for kw in keywords):
            return label
    return _UNCATEGORIZED


def _deduplicate(items: list[dict], sim_threshold: float = 0.92) -> list[dict]:
    """
    Remove near-duplicate entries from a list of retrieval results.
    Two items are considered duplicates when their stored similarity scores
    differ by < 0.02 AND they share the same theme — a cheap proxy for
    textual overlap without a second embedding call.
    """
    seen: list[dict] = []
    for item in items:
        is_dup = False
        for kept in seen:
            if (
                item.get("theme") == kept.get("theme")
                and abs(item["similarity"] - kept["similarity"]) < 0.02
                and item["text"][:80] == kept["text"][:80]
            ):
                is_dup = True
                break
        if not is_dup:
            seen.append(item)
    return seen


def cluster_patterns(
    repo_key: str,
    topic: str = "general code quality",
    n_results: int = 40,
    temporary: bool = False,
    namespace: str | None = None,
    min_cluster_size: int = 1,
) -> list[dict]:
    """
    Retrieve review comments and cluster them into named, deduplicated themes.

    Returns a list of cluster dicts, each with:
      - theme: str             — e.g. "Error Handling"
      - count: int             — number of distinct instances
      - confidence: float      — mean similarity score
      - examples: list[str]    — top-3 verbatim snippets
      - files: list[str]       — distinct files where this pattern appeared
    """
    raw = query_similar(
        repo_key,
        topic,
        n_results=n_results,
        temporary=temporary,
        namespace=namespace,
    )

    if not raw:
        return []

    # Assign theme to each result
    for item in raw:
        item["theme"] = _assign_theme(item["text"])

    # Deduplicate across all items
    raw = _deduplicate(raw)

    # Group by theme
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in raw:
        buckets[item["theme"]].append(item)

    clusters = []
    for theme, items in sorted(buckets.items(), key=lambda x: -len(x[1])):
        if len(items) < min_cluster_size:
            continue

        # Extract file paths from metadata
        files = list({
            m.get("metadata", {}).get("file", "")
            for m in items
            if m.get("metadata", {}).get("file")
        })

        clusters.append({
            "theme": theme,
            "count": len(items),
            "confidence": round(sum(i["similarity"] for i in items) / len(items), 4),
            "examples": [i["text"][:300] for i in items[:3]],  # Top 3, capped at 300 chars
            "files": sorted(files)[:10],  # Top 10 files
        })

    return clusters
