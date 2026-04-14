import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from storage import (
    query_similar,
    get_collection_stats,
    list_all_repos,
    index_prs,
    repo_is_indexed_permanently,
    repo_is_indexed_temporarily,
)
from inference import review_with_context, summarize_patterns
from fetcher import fetch_prs

app = Server("github-pr-review-context")

# ── Session state ─────────────────────────────────────────────────────────────
# Tracks the active repo and per-repo storage type for this server session.
_session: dict = {
    "active_repo": None,
    # Maps repo_key → "permanent" | "temporary"
    "storage_types": {},
}

STORAGE_CONSEQUENCES = """
**Permanent storage** 💾
  - PR data is embedded and saved to disk (ChromaDB).
  - Available instantly on every future session — no re-fetching needed.
  - Disk usage: ~5–20 MB per repo (60 PRs).
  - Best for repos you'll query repeatedly.

**Temporary storage** ⚡
  - PR data is embedded and kept in memory only.
  - Faster to set up, zero disk usage.
  - Lost when the MCP server restarts (i.e., when you close/reopen Antigravity).
  - Best for one-off exploration of a repo you won't revisit.
"""


def _resolve_repo(arguments: dict) -> str:
    repo = arguments.get("repo") or _session["active_repo"]
    if not repo:
        raise ValueError(
            "No repo specified and no active repo set. "
            "Use ensure_repo_ready first, or pass 'repo' explicitly."
        )
    return repo

def _is_temporary(repo_key: str) -> bool:
    return _session["storage_types"].get(repo_key) == "temporary"


# ── Tool definitions ──────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── Smart repo management ─────────────────────────────────────────
        types.Tool(
            name="ensure_repo_ready",
            description=(
                "Smart repo loader. Call this whenever a user mentions a repo they want to work with. "
                "It checks if the repo is already indexed locally. "
                "If yes → activates it instantly. "
                "If no and storage is not specified → explains consequences and asks the user to choose. "
                "If storage is specified → fetches from GitHub, indexes, and activates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "owner/repo, e.g. psf/black",
                    },
                    "storage": {
                        "type": "string",
                        "enum": ["permanent", "temporary"],
                        "description": (
                            "Storage choice. Omit to trigger the explanation prompt. "
                            "Pass 'permanent' or 'temporary' once the user has decided."
                        ),
                    },
                    "pages": {
                        "type": "integer",
                        "default": 2,
                        "description": "Pages of 30 PRs each to fetch (only used when indexing).",
                    },
                },
                "required": ["repo"],
            },
        ),
        types.Tool(
            name="set_active_repo",
            description=(
                "Switch the active repo context to a different already-indexed repo. "
                "Use ensure_repo_ready instead if the repo might not be indexed yet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/repo to switch to"},
                },
                "required": ["repo"],
            },
        ),
        types.Tool(
            name="list_indexed_repos",
            description="List all repos indexed locally (both permanent and temporary), with storage type and document count.",
            inputSchema={"type": "object", "properties": {}},
        ),
        # ── Core review tools ─────────────────────────────────────────────
        types.Tool(
            name="semantic_search_reviews",
            description="Search past PR review comments semantically. Give it a code snippet, error, or concept.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/repo — omit to use the active session repo"},
                    "query": {"type": "string", "description": "Code snippet, concept, or question to search for"},
                    "n_results": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="review_code_with_history",
            description=(
                "Full AI code review grounded in this repo's historical PR review patterns. "
                "Pass a diff or code snippet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/repo — omit to use the active session repo"},
                    "code": {"type": "string", "description": "Diff or code to review"},
                },
                "required": ["code"],
            },
        ),
        types.Tool(
            name="get_team_review_patterns",
            description="What does this team commonly flag in code reviews? Returns top patterns from past PRs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/repo — omit to use the active session repo"},
                    "topic": {"type": "string", "default": "general code quality"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_index_stats",
            description="Check how many PR documents are indexed for a repo and what storage type it uses.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/repo — omit to use the active session repo"},
                },
                "required": [],
            },
        ),
    ]


# ── Tool execution ────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict):

    # ── ensure_repo_ready: the smart loader ──────────────────────────────
    if name == "ensure_repo_ready":
        repo = arguments["repo"]
        storage = arguments.get("storage")
        pages = arguments.get("pages", 2)

        # 1. Already indexed permanently?
        if repo_is_indexed_permanently(repo):
            _session["active_repo"] = repo
            _session["storage_types"][repo] = "permanent"
            stats = get_collection_stats(repo, temporary=False)
            return [types.TextContent(type="text", text=(
                f"✅ **{repo}** is already indexed permanently on disk.\n"
                f"📄 {stats['total_documents']} documents loaded and ready.\n"
                f"Active repo set to `{repo}`."
            ))]

        # 2. Already indexed temporarily (this session)?
        if repo_is_indexed_temporarily(repo):
            _session["active_repo"] = repo
            _session["storage_types"][repo] = "temporary"
            stats = get_collection_stats(repo, temporary=True)
            return [types.TextContent(type="text", text=(
                f"✅ **{repo}** is already indexed in memory (this session).\n"
                f"📄 {stats['total_documents']} documents loaded and ready.\n"
                f"Active repo set to `{repo}`."
            ))]

        # 3. Not indexed anywhere — need to fetch. Ask if no storage chosen.
        if storage is None:
            return [types.TextContent(type="text", text=(
                f"📦 **{repo}** is not indexed yet.\n\n"
                f"How would you like to store it?\n\n"
                f"{STORAGE_CONSEQUENCES}"
                f"Reply with **permanent** or **temporary** and I'll fetch and index it now "
                f"(fetches up to {pages * 30} PRs)."
            ))]

        # 4. User has chosen — fetch and index
        temporary = (storage == "temporary")
        storage_label = "temporary (in-memory)" if temporary else "permanent (disk)"

        status_msg = (
            f"⏳ Fetching up to {pages * 30} PRs from **{repo}** on GitHub...\n"
            f"Storage: {storage_label}\nThis takes ~30–60 seconds."
        )

        try:
            prs = fetch_prs(*repo.split("/", 1), pages=pages)
            count = index_prs(repo, prs, temporary=temporary)
        except Exception as e:
            return [types.TextContent(type="text", text=f"❌ Failed to index {repo}: {e}")]

        _session["active_repo"] = repo
        _session["storage_types"][repo] = storage

        consequence_note = (
            "\n⚠️ This repo's index will be **lost when Antigravity restarts**. "
            "Re-run with `storage=permanent` anytime to persist it."
            if temporary else
            "\n💾 This repo's index is **saved to disk** and will be available in all future sessions."
        )

        return [types.TextContent(type="text", text=(
            f"✅ **{repo}** indexed successfully! [{storage_label}]\n"
            f"📄 {count} documents from {len(prs)} PRs.\n"
            f"Active repo set to `{repo}`."
            f"{consequence_note}"
        ))]

    # ── set_active_repo ───────────────────────────────────────────────────
    if name == "set_active_repo":
        repo = arguments["repo"]
        if not repo_is_indexed_permanently(repo) and not repo_is_indexed_temporarily(repo):
            return [types.TextContent(type="text", text=(
                f"❌ **{repo}** is not indexed yet. Use `ensure_repo_ready` first."
            ))]
        previous = _session["active_repo"]
        _session["active_repo"] = repo
        msg = f"✅ Active repo switched to: **{repo}**"
        if previous and previous != repo:
            msg += f"\n(previously: `{previous}`)"
        return [types.TextContent(type="text", text=msg)]

    # ── list_indexed_repos ────────────────────────────────────────────────
    if name == "list_indexed_repos":
        repos = list_all_repos()
        active = _session["active_repo"]
        if not repos:
            return [types.TextContent(type="text", text=(
                "No repos indexed yet. Ask about a repo and I'll use `ensure_repo_ready` to set it up."
            ))]
        lines = ["**Indexed repos:**\n"]
        for r in repos:
            marker = " ← active" if r["repo"] == active else ""
            icon = "💾" if r["storage"] == "permanent" else "⚡"
            lines.append(f"- {icon} `{r['repo']}` ({r['total_documents']} docs, {r['storage']}){marker}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── Core tools (resolve repo + route to correct storage) ──────────────
    try:
        repo = _resolve_repo(arguments)
    except ValueError as e:
        return [types.TextContent(type="text", text=f"❌ {e}")]

    temporary = _is_temporary(repo)

    if name == "semantic_search_reviews":
        results = query_similar(repo, arguments["query"], arguments.get("n_results", 8), temporary=temporary)
        return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

    elif name == "review_code_with_history":
        context = query_similar(repo, arguments["code"], n_results=10, temporary=temporary)
        review = review_with_context(arguments["code"], context, repo)
        return [types.TextContent(type="text", text=review)]

    elif name == "get_team_review_patterns":
        topic = arguments.get("topic", "general code quality")
        context = query_similar(repo, topic, n_results=20, temporary=temporary)
        summary = summarize_patterns(context, repo)
        return [types.TextContent(type="text", text=summary)]

    elif name == "get_index_stats":
        stats = get_collection_stats(repo, temporary=temporary)
        return [types.TextContent(type="text", text=json.dumps(stats, indent=2))]

    return [types.TextContent(type="text", text="Unknown tool")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())