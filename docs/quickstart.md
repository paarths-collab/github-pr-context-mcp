# Quick Start and Usage

## Quickstart

### Prerequisites
- Python 3.10+
- [GitHub Personal Access Token](GUIDE_GITHUB_TOKEN.md) — `repo` scope for private repos, `public_repo` for public only
- An LLM API key — multiple providers supported, see [LLM Configuration](llm-configuration.md)

Render hosting is **UPCOMING**. Once available, users will register a Gmail address once and use a bearer token.

---

## Installation & setup

### 🚀 Zero-Setup (uvx / pipx / npx)
The fastest way to use the server. No cloning required. Just run one of these commands directly in your terminal or use them in your IDE's MCP settings:

**Using uvx (Recommended for speed):**
```bash
uvx github-pr-context-mcp
```

**Using pipx (Recommended for stability):**
```bash
pipx run github-pr-context-mcp
# To install permanently as a global command:
pipx install github-pr-context-mcp
```

**Using npx (Smithery bridge):**
```bash
npx -y @smithery/cli run github-pr-context-mcp
```

---

### ⚠️ Manual Installation (Git Clone / Advanced)
> [!WARNING]
> Running from a git clone is **only recommended for developers** contributing to this project. For general use, please use the `pipx` method above.

If you have cloned the repository for development:
1. Create a virtual environment: `python -m venv .venv`
2. Activate it and install: `pip install -e .`
3. Run automatic setup: `python scripts/install_clients.py`

### 📦 Manual PIP Install
If you prefer standard pip:

```bash
pip install github-pr-context-mcp
# or from this directory
pip install -e .
```
Once installed, you can use the command `github-pr-context-mcp` in any terminal or IDE config.

### 🛠️ Manual Integration
If you prefer managing the lifecycle yourself:

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
python scripts/indexer.py psf/black --pages 2
# Or use full URLs: python scripts/indexer.py https://github.com/psf/black
# --pages: 1-10, default 2. More pages = better context but slower.
```

### Example prompts

```
"Review this code using the psf/black repo's history"
"Generate a new error handler for my fastmcp app using the tiangolo/fastapi history"
"What does the vercel/next.js team commonly flag in reviews?"
"Switch to https://github.com/tiangolo/fastapi context"
"Find past review comments about error handling in my active repo"
"List all repos I've indexed"
"Create a .cursorrules file for this repository using its history"
```

---

## 🧠 Institutional Memory (New)

The biggest bottleneck in AI coding is the agent "forgetting" your team's unsaid rules (e.g., "always use early returns," "avoid magic strings").

Instead of re-analyzing history on every task, you can **bake the team's brain** into your IDE:

1.  **Generate the rules:**
    `"Generate a .cursorrules file for this repo"`
2.  **The file is saved:** A `.cursorrules` (or `CLAUDE.md`) is written to your project root.
3.  **Automatic Enforcement:** Any future `generate_code_from_history` call will automatically detect this file and use it as hard constraints.

This makes your AI agent behave like a senior engineer who has been at the company for years.

---

## 🛠️ Changing Tokens & Settings
Need to swap your GitHub token or LLM provider? See the [**Configuration Guide**](guides/configuration.md).

---

Back to [README](../README.md)
