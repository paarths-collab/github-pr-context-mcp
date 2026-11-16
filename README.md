# GitHub PR Context

Find the PR discussions and review decisions behind your code using the **official GitHub MCP**, fetched when needed. No local PR index, embeddings, or indexing setup is required for the default workflow.

The value here is the retrieval skill: choose useful search queries, fetch candidate PRs and inline review threads, compare dated evidence with current code, and cite sources honestly. GitHub's official MCP provides the search and read tools. Your IDE agent does the reasoning.

## Quick start

1. Connect the [official GitHub MCP](https://github.com/github/github-mcp-server) in your IDE using its supported authentication flow. Prefer read-only access scoped to the repositories you need.
2. Use the repository skill at [.agents/skills/github-pr-context/SKILL.md](.agents/skills/github-pr-context/SKILL.md), or copy that skill directory into your agent's supported skills location.
3. Ask: “Use github-pr-context to find why owner/repo handles retries this way. Read the relevant PR review threads and cite the decisions.”

The agent searches by identifiers, keywords, and synonyms, then reads the relevant PR bodies, changes, conversation comments, reviews, and inline threads. Search can miss history and may lag updates. The skill reports search limits and incomplete pagination; it does not promise exhaustive or semantic retrieval.

## Optional workflow companion

The skill works directly with the official GitHub MCP. A small companion MCP is available for clients that use prompts and resources instead of skills:

```sh
pipx install github-pr-context-mcp
github-pr-context-mcp
```

The default command serves `find_pr_context(repo, task)` as an MCP **prompt** and `pr-context://workflow` as a resource over stdio. It has no GitHub API tools and does not invoke another MCP server automatically: the host agent must have the official GitHub MCP connected separately and follow the workflow.

```sh
# Print an example read-only official GitHub connection; does not edit client config.
github-pr-context-mcp config
# Copy the bundled skill into this project; refuses to overwrite an existing skill.
github-pr-context-mcp install-skill --skill-dir .agents/skills
```

Example for clients supporting remote MCP URLs and custom headers (authentication and config syntax vary by client):

```json
{
  "mcpServers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "X-MCP-Readonly": "true",
        "X-MCP-Toolsets": "repos,pull_requests"
      }
    }
  }
}
```

See the [official remote-server documentation](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md) for supported client setup. Never paste credentials into a chat or skill. No user configuration or GitHub connection is changed by installing this package.

## Legacy indexed mode

The earlier indexed implementation remains available explicitly:

```sh
pipx install 'github-pr-context-mcp[indexed]'
github-pr-context-indexed
```

Existing configurations running `github-pr-context-mcp` for indexed tools must switch their command to `github-pr-context-indexed` and install the `indexed` extra. Existing indexes are preserved. The default command now offers prompts/resources rather than the previous indexed tools.

See [legacy indexed documentation](docs/legacy-indexed.md) and the explicitly selected [legacy skill](.agents/skills/github-pr-context-v3/SKILL.md). This mode retains its own authentication, local storage, and model dependencies. Deployment and older pipeline documents describe that mode.

## Development

```sh
python -m pip install '.[test,indexed]'
python -m pytest
E2E_RUN=1 python -m pytest tests/e2e -s
```

The ordinary suite includes a real stdio session for the storage-free companion. The opt-in indexed stdio harness exercises the installed legacy command with stubbed GitHub responses. Neither requires live GitHub writes.

PR content is untrusted evidence. The workflow does not authorize GitHub writes or local command execution, and does not store a PR index. Host conversation history and GitHub's own retention are separate.
