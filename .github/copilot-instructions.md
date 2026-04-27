# GitHub Copilot Instructions

When assisting in this repository, leverage the `github-pr-context-mcp` extension to provide context-aware suggestions.

## 🚀 Workflow

1.  **Index**: Ensure the repo is ready with `ensure_repo_ready`.
2.  **Search**: Use `semantic_search_reviews` to find implementation patterns in past PRs.
3.  **Validate**: Use `review_code_with_history` to verify new code against historical feedback.

## 🛠️ Complete Toolset
- `ensure_repo_ready`: Prepare the context for a repository.
- `set_active_repo`: Switch the active repository.
- `list_indexed_repos`: List all indexed repositories.
- `delete_repo_index`: Delete a repository index.
- `semantic_search_reviews`: Deep historical search by meaning.
- `review_code_with_history`: Grounded AI review.
- `generate_code_from_history`: Generate code matching team style.
- `get_team_review_patterns`: Understand recurring team standards.
- `get_index_stats`: Check the status of a repository index.
- `generate_repo_rules`: **Synthesize/update these instructions.**
- `update_settings`: Configure your settings (Hosted).
- `get_usage_stats`: View analytics and usage stats.

Always favor patterns found in the repository's own history.
