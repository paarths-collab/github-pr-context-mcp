# Quick Start and Usage

## Quickstart

### Prerequisites
- Python 3.10+
- [GitHub Personal Access Token](GUIDE_GITHUB_TOKEN.md) — `repo` scope for private repos, `public_repo` for public only.

> [!NOTE]
> This server follows a **Pure Context** model. You do **NOT** need to provide LLM API keys (Groq, OpenAI, etc.) to the server. Your IDE agent (Cursor, Claude, etc.) will use its own intelligence to process the data provided by this server.

---

## Installation & Setup

### 🚀 Recommended (uvx / pipx)
The fastest way to use the server. No cloning required.

**Using uvx:**
```bash
uvx github-pr-context-mcp
```

**Using pipx:**
```bash
pipx install github-pr-context-mcp
```

---

## Usage

### 1. Onboard a Repository
Simply ask your IDE agent:
`"Review this code using the history of owner/repo"`

The server will:
1. Check if `owner/repo` is indexed locally.
2. If not, it will ask if you want **permanent** (disk) or **temporary** (memory) storage.
3. It will fetch and index the PR history in the background.

### 2. Pure Context Tools
All tools return raw JSON "historical facts". Your agent will automatically read these and use them to guide its work.

**Example Tools:**
- `review_code_with_history`: Returns past review comments related to your code.
- `get_repo_rules_material`: Returns a summary of team standards for writing `.cursorrules`.
- `generate_code_from_history`: Returns past implementation patterns for your task.

### 3. Example Prompts
- `"Review this snippet based on the team's historical feedback in this repo."`
- `"What are the common architectural patterns in this project's PR history?"`
- `"Help me write a .cursorrules file by analyzing the past 50 PRs."`
- `"Search the PR history for how we handle database migrations."`

---

## Storage: Permanent vs Temporary

| | Permanent 💾 | Temporary ⚡ |
|---|---|---|
| **Stored** | Disk (ChromaDB) | RAM only |
| **Survives restart** | ✅ Yes | ❌ No |
| **Disk usage** | ~5–20 MB per repo | 0 MB |
| **Best for** | Repos you query often | One-off exploration |

---

## 🛠️ Configuration
If you need to update your `GITHUB_TOKEN`, set it as an environment variable in your IDE's MCP settings or your system shell.

---

Back to [README](../README.md)
