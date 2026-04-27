# Claude Code / Claude Desktop Instructions

This repository is optimized for use with the `github-pr-context-mcp` server. Use the available tools to ground your responses in real repository history.

## 📋 Recommended Workflow

- **Check State**: Call `list_indexed_repos` to see what's already indexed.
- **Activate**: Use `ensure_repo_ready(repo="paarths-collab/github-pr-context-mcp")` to start.
- **Contextualize**: For any coding task, search history first: `semantic_search_reviews(query="your task details")`.
- **Align**: Use `get_team_review_patterns` to understand specific team preferences (e.g., error handling, logging).

## 🧰 Full Tool Inventory

| Tool | Action |
|---|---|
| `ensure_repo_ready` | Index a repository and ensure it is ready for queries |
| `set_active_repo` | Switch between multiple indexed repositories |
| `list_indexed_repos` | View all repos currently in local/temporary storage |
| `delete_repo_index` | Free up disk space by clearing repository indices |
| `semantic_search_reviews` | Search past review comments by meaning, not just keywords |
| `review_code_with_history` | AI review grounded in team review memory |
| `generate_code_from_history`| Write code grounded in past commits & team style |
| `get_team_review_patterns` | Summarize recurring team standards |
| `get_index_stats` | Verify if a repo index is complete (doc count) |
| `generate_repo_rules` | **Synthesize/update these instructions from history** |
| `update_settings` | Update tokens/LLM keys (Hosted mode only) |
| `get_usage_stats` | View adoption metrics and unique user counts |

Ground every decision in evidence from past PRs.
