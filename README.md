# GitHub PR Review Context MCP (v0.3.0)
<!-- mcp-name: io.github.paarths-collab/github-pr-context-mcp -->

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Protocol](https://img.shields.io/badge/Protocol-MCP-green)
![Version](https://img.shields.io/badge/version-0.2.10-green)
![Architecture](https://img.shields.io/badge/Architecture-Pure--Context-blue)

**A high-performance "Pure Context" engine for AI code review, providing raw historical PR data directly to your IDE agent.**

---

## ⚡ Pure Context Model (v0.2.10)

This server has been refactored into a **Pure Context Engine**.

Unlike traditional RAG servers that handle inference internally, this server **offloads all reasoning and code generation to your IDE agent** (Cursor, Claude, Windsurf, etc.). This ensures:
1. **No External LLM Costs**: The server doesn't need your Groq/Cerebras/OpenAI keys.
2. **Maximum Intelligence**: You use the full power of your IDE's frontier model to process the raw repository history.
3. **Privacy**: Reasoning happens inside your agent's context window, not on an external server.

---

## Overview

GitHub PR Review Context MCP gives AI assistants institutional review memory.

It fetches your repository's PR history (descriptions, review threads, comments), transforms them into searchable documents, and provides high-density **Context Materials** to your IDE agent.

### Core Value

- **Historical Evidence**: Tools return raw JSON "historical facts" (what reviewers cared about in the past).
- **Team Alignment**: Your IDE agent uses this evidence to match your team's specific standards and architectural patterns.
- **High Performance**: Optimized for fast retrieval and background indexing to prevent tool timeouts.

---

## Key Capabilities

| Capability | What It Delivers |
|---|---|
| **Historical Retrieval** | Semantic search across prior PR comments and review summaries. |
| **Code Review Material** | Raw JSON context for the agent to perform grounded code reviews. |
| **Rules Material** | High-density data for the agent to synthesize `.cursorrules` or `CLAUDE.md`. |
| **Grounded Generation** | Context materials for generating code that matches team style. |
| **Namespace Isolation** | Strict isolation between users/teams using Gmail-based identity. |

---

## 🚀 Quick Start

### 🚀 Recommended Installation (uvx / pipx)

```bash
# Run instantly
uvx github-pr-context-mcp

# OR Install permanently
pipx install github-pr-context-mcp
```

### 🛠️ Configuration

The only required configuration is your GitHub token.

**Environment Variable:**
`GITHUB_TOKEN=ghp_your_token_here`

---

## 🧰 Tools Reference (Pure Context)

The server provides tools that return **raw JSON context objects**. The IDE agent then uses its own intelligence to process this data.

| Tool | Action | typical Use Case |
|---|---|---|
| `ensure_repo_ready` | Index a repo and ensure it's ready. | Onboarding a new repository. |
| `review_code_with_history` | Get historical review material for a snippet. | "Review this code based on team history." |
| `get_repo_rules_material` | Get material to write `.cursorrules`. | "Write a rules file for this repo." |
| `get_team_review_patterns` | Get raw patterns for summarization. | "What are the common review themes?" |
| `generate_code_from_history` | Get context for grounded generation. | "Write this feature in our team's style." |
| `semantic_search_reviews` | Search past PR comments by meaning. | Manual history lookup. |
| `list_indexed_repos` | View all currently indexed repositories. | Storage management. |

---

## 📖 Documentation

- 🏗️ [**Architecture & Pipeline**](docs/architecture.md) — How the Pure Context engine works.
- 🛠️ [**Quick Start Guide**](docs/quickstart.md) — Detailed setup instructions.
- 🚀 [**Roadmap**](docs/roadmap.md) — Future development plans.

---

## 📣 Community & Feedback

- **Feedback**: Please open an issue or start a discussion if you have ideas or encounter bugs.
- **Star ⭐**: If this tool saves you time, give it a star!

---

## ⚖️ License

MIT
