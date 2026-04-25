# Client Configurations

There are **3 ways** to connect to the GitHub PR Context MCP Server:

| Option | Who it's for | Requirements |
|---|---|---|
| **Option A — Hosted on Render** | Anyone, zero setup | Just a bearer token |
| **Option B — `uvx` / `pipx`** | Python-curious users | Python or `uv` installed |
| **Option C — Git Clone** | Developers | Python 3.10+ |

---

## Option A — Hosted on Render (Recommended)

> **Zero install. Zero config. No Python. No Node.**  
> You only need a bearer token. Email the server admin to get one.

All you do is paste this into your IDE config, substituting `YOUR_TOKEN` and the Render URL.

### Antigravity
Config file: `%APPDATA%\.gemini\antigravity\mcp_config.json`
```json
{
  "mcpServers": {
    "github-pr-context": {
      "type": "sse",
      "url": "https://YOUR-SERVICE.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

### Claude Desktop
Config file: `%APPDATA%\Claude\claude_desktop_config.json`
```json
{
  "mcpServers": {
    "github-pr-context": {
      "type": "sse",
      "url": "https://YOUR-SERVICE.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

### Claude Code
Config file: `.mcp.json` (project root or home directory)
```json
{
  "mcpServers": {
    "github-pr-context": {
      "type": "sse",
      "url": "https://YOUR-SERVICE.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

### Cursor
Config file: `%APPDATA%\Cursor\mcp.json`
```json
{
  "mcpServers": {
    "github-pr-context": {
      "type": "sse",
      "url": "https://YOUR-SERVICE.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

### Windsurf
Config file: `%APPDATA%\Codeium\windsurf\mcp_config.json`
```json
{
  "mcpServers": {
    "github-pr-context": {
      "type": "sse",
      "url": "https://YOUR-SERVICE.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

### VS Code Copilot
Config file: `.vscode/mcp.json`
```json
{
  "servers": {
    "github-pr-context": {
      "type": "sse",
      "url": "https://YOUR-SERVICE.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```
Enable `chat.mcp.enabled: true` in VS Code settings.

### OpenCode
Config file: `%APPDATA%\opencode\config.json`
```json
{
  "mcp": {
    "github-pr-context": {
      "type": "sse",
      "url": "https://YOUR-SERVICE.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

---

## Option B — `uvx` / `pipx` / `npx` (No git clone needed)

> **No git clone. GitHub token + LLM key go inside the IDE JSON config.**
> Pick the runner that matches the tools you already have installed.

| Runner | Requires | Best for |
|---|---|---|
| `uvx` | `uv` installed | Python users with modern tooling |
| `pipx` | `pipx` + Python 3.10+ | Python users with classic tooling |
| `npx mcp-remote` | Node.js | Any IDE, including ones without native SSE |

### How keys are configured (all sub-options)

Keys go inside the `env` block of the JSON config — no `.env` file needed:

```json
"env": {
  "GITHUB_TOKEN": "ghp_your_github_pat",
  "LLM_PROVIDER": "cerebras",
  "LLM_MODEL": "llama3.1-8b",
  "LLM_API_KEY": "your_llm_api_key"
}
```
**LLM options:** `cerebras` (free tier) | `openai` | `anthropic` | `gemini` | `groq` | `ollama`

---

### B1 — `uvx` (recommended Python runner)

No install step — `uvx` downloads and runs from GitHub automatically.

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/paarths-collab/github-pr-context-mcp",
        "github-pr-context-mcp"
      ],
      "env": {
        "GITHUB_TOKEN": "ghp_your_github_pat",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_llm_api_key",
        "CHROMA_PERSIST_DIR": "/your/home/dir/.github-pr-mcp-db"
      }
    }
  }
}
```
*Works for: Antigravity, Claude Desktop, Claude Code, Cursor, Windsurf*

For **VS Code Copilot** add `"type": "stdio"`:
```json
{
  "servers": {
    "github-pr-context": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/paarths-collab/github-pr-context-mcp",
        "github-pr-context-mcp"
      ],
      "env": {
        "GITHUB_TOKEN": "ghp_your_github_pat",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_llm_api_key",
        "CHROMA_PERSIST_DIR": "/your/home/dir/.github-pr-mcp-db"
      }
    }
  }
}
```

---

### B2 — `pipx` (classic Python runner)

First install once:
```bash
pipx install "git+https://github.com/paarths-collab/github-pr-context-mcp"
```

Then configure your IDE:

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "github-pr-context-mcp",
      "env": {
        "GITHUB_TOKEN": "ghp_your_github_pat",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_llm_api_key",
        "CHROMA_PERSIST_DIR": "/your/home/dir/.github-pr-mcp-db"
      }
    }
  }
}
```
*Works for: Antigravity, Claude Desktop, Claude Code, Cursor, Windsurf*

For **VS Code Copilot**:
```json
{
  "servers": {
    "github-pr-context": {
      "type": "stdio",
      "command": "github-pr-context-mcp",
      "env": {
        "GITHUB_TOKEN": "ghp_your_github_pat",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_llm_api_key",
        "CHROMA_PERSIST_DIR": "/your/home/dir/.github-pr-mcp-db"
      }
    }
  }
}
```

---

### B3 — `npx mcp-remote` (Node.js bridge to Render)

> Use this if your IDE **doesn't support native `type: sse`** yet.  
> Requires Node.js. Bridges directly to the Render-hosted server.  
> **No GitHub token or LLM key needed** — the server handles those.

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://YOUR-SERVICE.onrender.com/mcp",
        "--header",
        "Authorization:Bearer YOUR_TOKEN"
      ]
    }
  }
}
```
*Works for: Antigravity, Claude Desktop, Claude Code, Cursor, Windsurf*

For **VS Code Copilot**:
```json
{
  "servers": {
    "github-pr-context": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://YOUR-SERVICE.onrender.com/mcp",
        "--header",
        "Authorization:Bearer YOUR_TOKEN"
      ]
    }
  }
}
```



---

## Option C — Git Clone (Developer mode)

> **Full control. Local ChromaDB on your machine.**  
> Requires Python 3.10+.

### Step 1 — Clone and install

```bash
git clone https://github.com/paarths-collab/github-pr-context-mcp
cd github-pr-context-mcp
pip install -r requirements.txt
```

### Step 2 — Configure your keys via `.env`

```bash
cp .env.example .env
```

Then edit `.env`:

```env
GITHUB_TOKEN=ghp_your_github_pat        # from github.com/settings/tokens
LLM_PROVIDER=cerebras                   # or: openai | anthropic | gemini | groq | ollama
LLM_MODEL=llama3.1-8b
LLM_API_KEY=your_llm_api_key
CHROMA_PERSIST_DIR=./chroma_db
```

**Where to get API keys:**
- Cerebras (free): https://cloud.cerebras.ai
- OpenAI: https://platform.openai.com
- Anthropic: https://console.anthropic.com
- Gemini: https://aistudio.google.com
- Groq (free): https://console.groq.com/keys

### Step 3 — Configure your IDE

Replace `/absolute/path/to/github-pr-context-mcp` with your actual clone path.

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/entrypoints/local/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_github_pat",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_llm_api_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/github-pr-context-mcp/chroma_db"
      }
    }
  }
}
```

> **Tip:** Prefer passing keys in the IDE `env` block over the `.env` file — it avoids the file path lookup entirely.

### VS Code Copilot (git clone)
```json
{
  "servers": {
    "github-pr-context": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/github-pr-context-mcp/entrypoints/local/server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_github_pat",
        "LLM_PROVIDER": "cerebras",
        "LLM_MODEL": "llama3.1-8b",
        "LLM_API_KEY": "your_llm_api_key",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/github-pr-context-mcp/chroma_db"
      }
    }
  }
}
```

Enable `chat.mcp.enabled: true` in VS Code settings.
