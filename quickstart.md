# Quick Start and Usage

## Quickstart

### Prerequisites
- Python 3.10+
- [GitHub Personal Access Token](GUIDE_GITHUB_TOKEN.md) — `repo` scope for private repos, `public_repo` for public only
- An LLM API key — multiple providers supported, see [LLM Configuration](llm-configuration.md)

### Install

```bash
git clone https://github.com/paarths-collab/github-pr-context-mcp
cd github-pr-context-mcp
pip install -r requirements.txt
cp .env.example .env
# Fill in GITHUB_TOKEN + your chosen LLM key
```

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
# Or use full URLs: python indexer.py https://github.com/psf/black
# --pages: 1-10, default 2. More pages = better context but slower.
```

### Example prompts

```
"Review this code using the psf/black repo's history"
"What does the vercel/next.js team commonly flag in reviews?"
"Switch to https://github.com/tiangolo/fastapi context"
"Find past review comments about error handling in my active repo"
"List all repos I've indexed"
```

---

Back to [README](README.md)
