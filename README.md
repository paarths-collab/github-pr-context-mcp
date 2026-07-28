# GitHub PR Context MCP

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Protocol](https://img.shields.io/badge/Protocol-MCP-green)
![Version](https://img.shields.io/badge/version-0.3.0-green)

GitHub PR Context MCP is a **v0.3.0 pure-context** MCP server. It indexes GitHub pull-request history and returns relevant historical material to an IDE agent. The IDE agent, not this server, performs the reasoning, review, code generation, and file edits.

## How it works

```mermaid
flowchart LR
    U["Developer request"] --> A["IDE agent"]
    A --> M["GitHub PR Context MCP"]
    M --> AU["Local GitHub App Device Flow"]
    AU --> K["OS credential vault"]
    AU --> G["GitHub PR history"]
    M --> G["GitHub PR history"]
    G --> M
    M --> V["ChromaDB retrieval index"]
    V --> M
    M --> J["Historical-context JSON"]
    J --> A
    A --> O["Review, plan, code, or rules file"]
```

| Component | Responsibility |
| --- | --- |
| MCP server | Fetches, normalizes, embeds, stores, and retrieves PR history. |
| IDE agent | Chooses tools, interprets evidence, validates it against current code, and writes the answer or code. |
| Embedding model | Finds semantically related historical records. It is not the reasoning model. |

This split keeps model-provider configuration and final decisions in the IDE. The MCP server supplies evidence rather than a server-generated verdict.

## Install

The package and command name are both `github-pr-context-mcp`. Do not use the obsolete `github-pr-engine` command.

Install the current v0.3 source checkout:

```bash
pipx install .
github-pr-context-mcp --help
```

For an ephemeral source-run instead:

```bash
uvx --from . github-pr-context-mcp
```

When a v0.3 package release is available from your package index, use the same package name:

```bash
uvx github-pr-context-mcp
# or
pipx install github-pr-context-mcp
```

## Configure an IDE client

The supported v0.3 workflow is a free local stdio MCP with one product-owned public GitHub App. A configured official release includes the App's public Client ID. Each user installs that App only on the repositories they choose, approves GitHub once in a browser, and the resulting credential stays in their operating-system vault rather than source control, `.env`, or an MCP prompt.

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "github-pr-context-mcp"
    }
  }
}
```

After restarting the IDE, call `get_github_connection_status`. If needed, it gives the product App installation URL; choose only the repositories you want to share. Then call `begin_github_authorization`, approve the one-time code in GitHub, and call `complete_github_authorization`. [Full local GitHub App setup](docs/guides/github-app-device-flow.md).

Users do not create a GitHub App or paste a PAT. Do not put a GitHub App private key, client secret, access token, or refresh token in a downloadable MCP configuration.

### Release maintainer: configure the App once

Before publishing the package, create one public GitHub App, enable Device Flow, and put only its public Client ID and URL slug in [`auth/product_github_app.py`](auth/product_github_app.py). That file deliberately contains no secret. Users of the published release then need no GitHub configuration beyond installing and approving the App.

The repository-local [v3 skill](.agents/skills/github-pr-context-v3/SKILL.md) tells capable IDE agents when to retrieve context and when to reason or write themselves. Installed packages ship the same skill; install it into a workspace with:

```bash
github-pr-context-mcp install-skill --skill-dir .agents/skills
```

## Typical workflow

1. Ask the agent to call `ensure_repo_ready` with an `owner/repo` value and a storage choice.
2. Wait for `get_index_stats` to show indexed documents. Indexing starts in the background.
3. Ask the agent to retrieve evidence with the tool that fits the task.
4. The IDE agent compares that evidence with the current codebase and produces the review, implementation, tests, or rules file.

To refresh an existing repository, call `ensure_repo_ready(repo="owner/repo", refresh=true)`, then inspect `get_index_stats` again. Its `index_job` reports `queued`, `running`, `partial`, `ready`, `cancelled`, or `failed`, plus any extraction limits encountered during that run. A `partial` refresh has safely saved a cursor; run the refresh again to continue before its GitHub watermark advances.

### Upgrade existing local indexes

v0.2 and earlier used a different collection layout. Close any IDE clients running this MCP, then copy those local indexes once (the command leaves the old data as a backup):

```bash
github-pr-context-mcp migrate-storage --dry-run
github-pr-context-mcp migrate-storage
```

Restart the IDE and verify the migrated repository with `get_index_stats`. The migration intentionally does not reuse an old local timestamp as a GitHub `updatedAt` watermark, so the first v3 refresh remains safe.

### Storage choices

| Mode | Storage | Survives restart | Best for |
| --- | --- | --- | --- |
| `permanent` | Local ChromaDB data on disk | Yes | Repositories you revisit. |
| `temporary` | In-memory index | No | One-off investigation. |

## Tools

All history-oriented tools return retrieval material for the IDE agent to use. They do not use a chat-model provider to make the final decision.

| Tool group | Tools | What the IDE agent should do with the result |
| --- | --- | --- |
| GitHub connection | `get_github_connection_status`, `begin_github_authorization`, `complete_github_authorization`, `disconnect_github` | Obtain user-approved local GitHub access. Never provide a token to these tools. |
| Index lifecycle | `ensure_repo_ready`, `set_active_repo`, `list_indexed_repos`, `get_index_stats`, `delete_repo_index` | Select, prepare, inspect, or remove an index. |
| Historical search | `semantic_search_reviews`, `find_similar_errors`, `get_team_review_patterns` | Find evidence about previous reviews, failures, and team preferences. |
| Context for a task | `review_code_with_history`, `generate_code_from_history`, `generate_tests`, `static_analysis`, `suggest_refactors`, `security_check` | Reason over the returned context before reviewing, coding, testing, or suggesting changes. |
| Agent-instruction material | `get_repo_rules_material` | Synthesize a local `CLAUDE.md`, `.cursorrules`, or equivalent instructions in the IDE workspace. The MCP tool only returns material. |
| Hosted administration | `update_settings`, `get_usage_stats` | Manage hosted configuration or inspect usage where those features are enabled. |

## Accuracy and verification

Historical PR data is supporting evidence, not a substitute for checking the current repository. The IDE agent should:

- check index status before relying on a newly requested index;
- validate retrieved patterns against current code and project instructions; and
- distinguish an old review preference from a current requirement.

GitHub limits nested PR connections and the requested top-level page count. v3 records those limits as `truncated_connections` in indexed evidence and in `get_index_stats().index_job`; do not present partially extracted history as complete.

## Development

Install the package with its test extras, then run the suite:

```bash
python -m pip install ".[test]"
python -m pytest
```

## Documentation

- [Quick start](docs/quickstart.md)
- [GitHub App Device Flow](docs/guides/github-app-device-flow.md)
- [Architecture](docs/architecture.md)
- [Tool strategy](docs/tools_strategy.md)

## License

MIT
