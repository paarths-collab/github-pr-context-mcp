# Local integration

Use a local stdio server when you want PR context, Chroma data, and GitHub credentials to remain on your machine. This is the supported v0.3 deployment model.

## Install from this checkout

```bash
pipx install .
```

The command is `github-pr-context-mcp`.

## Cursor or Claude-style configuration

Use the client’s `mcpServers` object:

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "github-pr-context-mcp"
    }
  }
}
```

## VS Code Copilot configuration

Use the `servers` object in `.vscode/mcp.json`:

```json
{
  "servers": {
    "github-pr-context": {
      "type": "stdio",
      "command": "github-pr-context-mcp"
    }
  }
}
```

After restarting the client, call `get_github_connection_status`, open its product App installation URL if present, then call `begin_github_authorization` and `complete_github_authorization`. The GitHub App Device Flow stores the resulting credential in the operating-system vault; no GitHub token or App Client ID belongs in this client configuration.

The IDE client supplies its own reasoning model, so do not add `LLM_PROVIDER`, `LLM_MODEL`, or `LLM_API_KEY` to this MCP server configuration. Do not add a GitHub App client secret or private key either.

For the one-time user approval, expiry, and disconnect flow, see [Free local GitHub App flow](../guides/github-app-device-flow.md). A project maintainer creates and bundles the App once before publishing.

Back to [Integrations](index.md)
