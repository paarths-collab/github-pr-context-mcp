# Local Integration

Use this when you want storage to stay on the user's machine.

For complete per-IDE snippets, see [Client configurations](clients.md).

## VS Code Copilot

Create `.vscode/mcp.json`:

```json
{
  "servers": {
    "github-pr-context": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/entrypoints/local/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/github-pr-context-mcp/chroma_db"
      }
    }
  }
}
```

Enable `chat.mcp.enabled: true`.

## Cursor

Create `.cursor/mcp.json` or `%APPDATA%\\Cursor\\mcp.json`:

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/entrypoints/local/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/github-pr-context-mcp/chroma_db"
      }
    }
  }
}
```

## Other clients

Use the same stdio server path with the client's wrapper key:

- Antigravity: `mcpServers`
- Claude Desktop: `mcpServers`
- Claude Code: `mcpServers`
- Windsurf: `mcpServers`
- OpenCode: `mcp`
