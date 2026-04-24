# Integrations

All integrations use the same MCP stdio transport. Replace `/absolute/path/to` with your actual path.

## Client Quick Compare

| Client | Config File Location (Windows) | Config Key |
|---|---|---|
| Antigravity | `%APPDATA%\\.gemini\\antigravity\\mcp_config.json` | `mcpServers` |
| Claude Desktop | `%APPDATA%\\Claude\\claude_desktop_config.json` | `mcpServers` |
| Claude Code | `.mcp.json` (project/home) | `mcpServers` |
| Cursor | `%APPDATA%\\Cursor\\mcp.json` | `mcpServers` |
| Windsurf | `%APPDATA%\\Codeium\\windsurf\\mcp_config.json` | `mcpServers` |
| VS Code Copilot | `.vscode/mcp.json` | `servers` |
| OpenCode | `%APPDATA%\\opencode\\config.json` | `mcp` |

## Universal Server Template

Use this baseline and adapt only the wrapper key (`mcpServers`, `servers`, or `mcp`) per client.

```json
{
  "github-pr-context": {
    "command": "python",
    "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
    "env": {
      "GITHUB_TOKEN": "ghp_your_token",
      "LLM_PROVIDER": "cerebras",
      "LLM_MODEL": "llama3.1-8b",
      "LLM_API_KEY": "your_key",
      "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
    }
  }
}
```

<details>
<summary><strong>Expand Per-Client Config Snippets</strong></summary>

## Antigravity (Google)

**Mac/Linux:** `~/.gemini/antigravity/mcp_config.json`  
**Windows:** `%APPDATA%\\.gemini\\antigravity\\mcp_config.json`

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
      }
    }
  }
}
```

## Claude Desktop

**Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`  
**Windows:** `%APPDATA%\\Claude\\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
      }
    }
  }
}
```

## Claude Code

In your project root or home directory, create `.mcp.json`:

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
      }
    }
  }
}
```

## Cursor

**Mac/Linux:** `~/.cursor/mcp.json`  
**Windows:** `%APPDATA%\\Cursor\\mcp.json`

Or project-level: `.cursor/mcp.json` in your repo root.

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
      }
    }
  }
}
```

Then: Cursor Settings -> Features -> MCP -> enable.

## Windsurf

**Mac/Linux:** `~/.codeium/windsurf/mcp_config.json`  
**Windows:** `%APPDATA%\\Codeium\\windsurf\\mcp_config.json`

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
      }
    }
  }
}
```

## GitHub Copilot (VS Code)

Create `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "github-pr-context": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
      }
    }
  }
}
```

Enable: VS Code Settings -> `chat.mcp.enabled: true`. Requires VS Code 1.99+ with Copilot Chat.

## OpenCode

**Mac/Linux:** `~/.config/opencode/config.json`  
**Windows:** `%APPDATA%\\opencode\\config.json`

```json
{
  "mcp": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/chroma_db"
      }
    }
  }
}
```

</details>

---

Back to [README](README.md)
