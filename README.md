# GitHub PR Review Context MCP

> AI code review that remembers how YOUR team reviews code.

Most AI code reviewers have zero memory. They don't know your team flagged `async/await` misuse in 12 PRs last quarter. They don't know you've rejected the same error-handling pattern 5 times. This MCP fixes that.

It indexes your repo's PR history and gives your AI coding tool the institutional memory to review code the way your team actually does.

---

## Demo

<!-- Record a 15s screen capture: ask about a repo → paste diff → show the review response -->
<!-- Upload as assets/demo.gif -->
![demo](assets/demo.gif)

> "Review this diff using our team's review history" → finds past review comments, flags patterns your team has rejected before, responds in ~2 seconds.

---

## Who Is This For

### Open Source Contributors
You want to contribute to `psf/black` or `vercel/next.js` but don't know what the maintainers care about. The docs don't tell you that the team hates a certain pattern or that they've rejected the same approach 5 times. This tool reads 60 PRs of real review history and tells you exactly that — *before* you click "Create Pull Request."

### Junior Developers Joining a Team
You're new. You don't know why the team always asks for a certain error handling style or why they keep rejecting a particular abstraction. This tool reads your team's private PR history on day one and gives you the institutional memory that usually takes 6 months to build.

### Engineering Leads Tired of Repeating Themselves
You've left the same review comment 15 times. This tool learns from your past comments and catches the same issues automatically, before PRs hit human review.

### Developer Tool Builders
You want context-aware code review in your CI pipeline, IDE plugin, or internal dev platform. This MCP is a composable component you can plug in — any LLM, any client that supports MCP.

---

## Quickstart

### Prerequisites
- Python 3.10+
- [GitHub Personal Access Token](https://github.com/settings/tokens) — `repo` scope for private repos, `public_repo` for public only
- An LLM API key — multiple providers supported, see [Configuring Your LLM](#configuring-your-llm)

### Install

```bash
git clone https://github.com/paarths-collab/github-pr-context-mcp
cd github-pr-context-mcp
pip install -r requirements.txt
cp .env.example .env
# Fill in GITHUB_TOKEN + your chosen LLM key
```

---

## How to Use It

### Zero-setup (recommended)

Just talk to your AI tool. No manual indexing needed:

```
"I want to contribute to psf/black. Can you review this code using their history?"
```

The server will:
1. Check if `psf/black` is already indexed locally
2. If not — ask whether you want to store it **permanently** (disk, reusable) or **temporarily** (this session only, no disk usage)
3. Fetch and index the repo automatically
4. Review your code using real past PR patterns

### Storage: Permanent vs Temporary

| | Permanent 💾 | Temporary ⚡ |
|---|---|---|
| **Stored** | Disk (ChromaDB) | RAM only |
| **Survives restart** | ✅ Yes | ❌ No |
| **Disk usage** | ~5–20 MB per repo | 0 MB |
| **Best for** | Repos you query often | One-off exploration |

### Manual indexing (optional)

Pre-index before connecting to your AI tool:

```bash
python indexer.py psf/black --pages 2
# --pages: 1-10, default 2. More pages = better context but slower.
```

### Example prompts

```
"Review this code using the psf/black repo's history"
"What does the vercel/next.js team commonly flag in reviews?"
"Switch to tiangolo/fastapi context"
"Find past review comments about error handling in my active repo"
"List all repos I've indexed"
```

---

## Configuring Your LLM

Set `LLM_PROVIDER` and `LLM_MODEL` in your `.env`. No lock-in.

| Provider | `LLM_PROVIDER` | Example `LLM_MODEL` | Free Tier |
|---|---|---|---|
| **Cerebras** | `cerebras` | `llama3.1-8b` | ✅ [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| **Groq** | `groq` | `llama-3.1-8b-instant` | ✅ [console.groq.com](https://console.groq.com/keys) |
| **Gemini** | `gemini` | `gemini-1.5-flash` | ✅ [aistudio.google.com](https://aistudio.google.com) |
| **OpenAI** | `openai` | `gpt-4o-mini` | ❌ |
| **Anthropic** | `anthropic` | `claude-3-5-haiku-20241022` | ❌ |
| **Ollama** | `ollama` | `llama3.2` | ✅ fully local |

Set your key as `LLM_API_KEY` in `.env` — works for any provider:

---

## Integrations

All integrations use the same MCP stdio transport. Replace `/absolute/path/to` with your actual path.

### Antigravity (Google)

**Mac/Linux:** `~/.gemini/antigravity/mcp_config.json`  
**Windows:** `%APPDATA%\.gemini\antigravity\mcp_config.json`

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

### Claude Desktop

**Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`  
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

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

### Claude Code

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

### Cursor

**Mac/Linux:** `~/.cursor/mcp.json`  
**Windows:** `%APPDATA%\Cursor\mcp.json`

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

Then: Cursor Settings → Features → MCP → enable.

### Windsurf

**Mac/Linux:** `~/.codeium/windsurf/mcp_config.json`  
**Windows:** `%APPDATA%\Codeium\windsurf\mcp_config.json`

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

### GitHub Copilot (VS Code)

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

Enable: VS Code Settings → `chat.mcp.enabled: true`. Requires VS Code 1.99+ with Copilot Chat.

### OpenCode

**Mac/Linux:** `~/.config/opencode/config.json`  
**Windows:** `%APPDATA%\opencode\config.json`

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

---

## MCP Tools

| Tool | What It Does |
|---|---|
| `ensure_repo_ready` | Smart loader — checks local DB, prompts for storage choice if new, fetches + indexes automatically |
| `set_active_repo` | Switch context to a different already-indexed repo |
| `list_indexed_repos` | Show all repos indexed locally with storage type and doc count |
| `semantic_search_reviews` | Find past review comments similar to a code snippet or concept |
| `review_code_with_history` | Full AI review grounded in this repo's real PR history |
| `get_team_review_patterns` | Summarize top patterns this team flags in reviews |
| `get_index_stats` | Check document count and storage type for a repo |

---

## Project Structure

```
github-pr-context-mcp/
├── server.py              # MCP server — tool definitions + session state
├── indexer.py             # CLI: pre-index a repo without an AI tool
│
├── fetcher/               # GitHub API layer
│   ├── client.py
│   ├── queries.py
│   └── transform.py
│
├── storage/               # Vector DB layer
│   ├── encoder.py
│   ├── document_builder.py
│   └── vector_store.py
│
└── inference/             # LLM inference layer
    ├── providers.py       # Cerebras / Groq / Gemini / OpenAI / Anthropic / Ollama
    └── review.py
```

---

## Contributing

These are real gaps — not fake beginner tickets.

### Good First Issues

- [ ] **Record the demo GIF** — 15s screen capture: index a repo → paste a diff → show the response
- [ ] **Test Windsurf or OpenCode integration** — verify the config format and document any quirks
- [ ] **Add `--since` flag to indexer** — only fetch PRs after a given date
- [ ] **Add progress bar** — replace print statements in `indexer.py` with `tqdm`
- [ ] **Write tests for `fetcher/transform.py`** — mock a GraphQL response, assert the flattened output

### Bigger Contributions

- [ ] **Incremental indexing** — only fetch new PRs on re-run
- [ ] **File-aware retrieval** — when reviewing `auth/login.py`, prioritize past comments on the same file
- [ ] **Webhook mode** — auto-index new PRs as they merge
- [ ] **Streaming responses** — stream the review token-by-token

### How to Contribute

1. Fork the repo
2. Pick an issue or open one describing what you want to build
3. Open a PR — one thing per PR, keep it focused

---

## Roadmap

- [x] GraphQL fetching with review thread flattening
- [x] Local embeddings with ChromaDB persistence (permanent + temporary storage)
- [x] Swappable LLM backend (Cerebras / Groq / Gemini / OpenAI / Anthropic / Ollama)
- [x] Smart repo loader — no manual steps needed from the AI tool
- [x] Session memory — per-repo context switching
- [x] Integrations: Antigravity, Claude Desktop, Claude Code, Cursor, Windsurf, GitHub Copilot, OpenCode
- [ ] Incremental indexing
- [ ] File-aware retrieval
- [ ] Webhook auto-indexing
- [ ] Streaming inference

---

## License

MIT — use it, fork it, build on it.

---

*If this saved you a PR rejection, a ⭐ helps others find it.*
