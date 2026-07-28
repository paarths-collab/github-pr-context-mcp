# Client configurations

v0.3 uses the same local server command for every client:

```text
github-pr-context-mcp
```

The official local release needs no GitHub credential value in an IDE configuration. The IDE’s own model reasons over the returned context; the connected GitHub credential is stored in the OS vault after browser approval.

## Generic stdio shape

Most MCP clients use an `mcpServers` object:

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "github-pr-context-mcp",
      "env": {
        "CHROMA_PERSIST_DIR": "/optional/stable/path/chroma_db"
      }
    }
  }
}
```

`CHROMA_PERSIST_DIR` is optional. It selects a stable local location for permanent indexes.

## VS Code Copilot

VS Code uses `servers` rather than `mcpServers`:

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

Enable MCP support in the client after adding the configuration.

## Run from source without a global install

Use `uvx` with an absolute path to the current checkout. IDE clients do not reliably start in this repository, so `.` would point to the wrong project:

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/github-pr-context-mcp", "github-pr-context-mcp"]
    }
  }
}
```

After adding the configuration, restart the IDE and call `get_github_connection_status`. Install the returned product App URL on selected repositories, then use `begin_github_authorization` to approve it once. Do not configure a GitHub token, chat-model provider, GitHub App client secret, or GitHub App private key in these server blocks. That configuration belongs to the IDE agent or, for future hosting, a server-side secret manager.

See [Free local GitHub App flow](../guides/github-app-device-flow.md) for the one-time authorization steps.

Back to [Integrations](index.md)
