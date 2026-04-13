import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from embeddings import query_similar, get_collection_stats, list_all_repos
from cerebras_review import review_with_context, summarize_patterns

app = Server("github-pr-review-context")

# --- Session Memory ---
# Stores the active repo for the current session so users don't have
# to specify it on every call when working within one repo context.
_session: dict = {
    "active_repo": None,
}

def _resolve_repo(arguments: dict) -> str:
    """Get repo from arguments, or fall back to the active session repo."""
    repo = arguments.get("repo") or _session["active_repo"]
    if not repo:
        raise ValueError(
            "No repo specified and no active repo set. "
            "Use set_active_repo first, or pass 'repo' explicitly."
        )
    return repo


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── Session management ────────────────────────────────────────────
        types.Tool(
            name="set_active_repo",
            description=(
                "Set the active repo for this session. After calling this, "
                "all other tools will use this repo by default so you don't "
                "need to specify it every time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "owner/repo to set as active, e.g. psf/black",
                    },
                },
                "required": ["repo"],
            },
        ),
        types.Tool(
            name="list_indexed_repos",
            description=(
                "List all repos currently indexed in ChromaDB, with document counts. "
                "Use this to see what's available before switching context."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Core tools ────────────────────────────────────────────────────
        types.Tool(
            name="semantic_search_reviews",
            description=(
                "Search past PR review comments semantically. "
                "Give it a code snippet, error, or concept and it returns the most "
                "relevant past reviewer feedback from this repo."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "owner/repo — omit to use the active session repo",
                    },
                    "query": {
                        "type": "string",
                        "description": "Code snippet, concept, or question to search for",
                    },
                    "n_results": {
                        "type": "integer",
                        "default": 8,
                        "description": "Number of similar past comments to return",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="review_code_with_history",
            description=(
                "Do a full AI code review using this repo's historical review patterns. "
                "Pass a diff or code snippet. Returns review comments grounded in how "
                "THIS team reviews code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "owner/repo — omit to use the active session repo",
                    },
                    "code": {
                        "type": "string",
                        "description": "Diff or code to review",
                    },
                },
                "required": ["code"],
            },
        ),
        types.Tool(
            name="get_team_review_patterns",
            description=(
                "What does this team commonly flag in code reviews? "
                "Returns top patterns extracted from past review comments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "owner/repo — omit to use the active session repo",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Optional: focus on a specific area like 'error handling' or 'types'",
                        "default": "general code quality",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_index_stats",
            description="Check how many PR documents are indexed for a repo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "owner/repo — omit to use the active session repo",
                    },
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):

    # ── Session management ────────────────────────────────────────────────
    if name == "set_active_repo":
        repo = arguments["repo"]
        previous = _session["active_repo"]
        _session["active_repo"] = repo
        msg = f"✅ Active repo set to: **{repo}**"
        if previous and previous != repo:
            msg += f"\n(previously: {previous})"
        return [types.TextContent(type="text", text=msg)]

    if name == "list_indexed_repos":
        repos = list_all_repos()
        active = _session["active_repo"]
        if not repos:
            return [types.TextContent(type="text", text="No repos indexed yet. Run indexer.py first.")]
        lines = ["**Indexed repos:**\n"]
        for r in repos:
            marker = " ← active" if r["repo"] == active else ""
            lines.append(f"- `{r['repo']}` ({r['total_documents']} docs){marker}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── Core tools (all fall back to active session repo) ─────────────────
    try:
        repo = _resolve_repo(arguments)
    except ValueError as e:
        return [types.TextContent(type="text", text=f"❌ {e}")]

    if name == "semantic_search_reviews":
        results = query_similar(repo, arguments["query"], arguments.get("n_results", 8))
        return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

    elif name == "review_code_with_history":
        context = query_similar(repo, arguments["code"], n_results=10)
        review = review_with_context(arguments["code"], context, repo)
        return [types.TextContent(type="text", text=review)]

    elif name == "get_team_review_patterns":
        topic = arguments.get("topic", "general code quality")
        context = query_similar(repo, topic, n_results=20)
        summary = summarize_patterns(context, repo)
        return [types.TextContent(type="text", text=summary)]

    elif name == "get_index_stats":
        stats = get_collection_stats(repo)
        return [types.TextContent(type="text", text=json.dumps(stats, indent=2))]

    return [types.TextContent(type="text", text="Unknown tool")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())