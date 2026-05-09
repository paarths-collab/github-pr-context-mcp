# 🚀 GitHub PR Context Tools

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Protocol](https://img.shields.io/badge/Protocol-MCP-green)
![Version](https://img.shields.io/badge/version-0.3.0-green)
![Downloads](https://img.shields.io/badge/downloads-3k+-blue)

This repository provides two ways to leverage your GitHub PR history in your AI workflows. Choose the tool version that matches your architecture.

---

## 💎 GitHub PR Context (v0.3.0)
**The Hardened Pure Context Retrieval Engine.**

Designed for users who want maximum speed and reliability by offloading reasoning to their IDE agent (Cursor, Claude Desktop, Windsurf). It focuses exclusively on high-performance retrieval and data pruning.

### ✨ Key Features (v0.3.0):
- **Asynchronous Hardening**: Built with `tenacity` retries and non-blocking I/O for heavy repository indexing.
- **Optimized for IDE Agents**: Prunes redundant metadata and strips useless fields to save token space in your prompt.
- **Modern Infrastructure**: Uses SQLite-backed cursors for progress tracking and global thread locks for data safety.
- **Pure Context**: No built-in LLM dependencies; feeds raw, high-quality historical patterns directly to your primary agent.

### 🛠 Installation (Pre-release Source Install)
> [!NOTE]
> **v0.3.0 is currently in pre-release and is not yet live on PyPI.** 
> To use the hardened v0.3.0 engine today, you must install it from source:
>
> 1. **Fork** this repository to your own account on GitHub.
> 2. **Clone and Checkout** the release branch:
>    ```bash
>    git clone https://github.com/YOUR_USERNAME/github-pr-context-mcp.git
>    cd github-pr-context-mcp
>    git checkout v0.3.0-hardening
>    ```
> 3. **Install via pipx** (the whole command):
>    ```bash
>    pipx install -e .
>    ```
>    *This links the `github-pr-context-mcp` command to your local repository folder.*
>
> 4. **Verify Version**:
>    ```bash
>    github-pr-context-mcp --help
>    ```

---

## 🏛 Legacy Context Agent (v0.2.9)
**The Integrated Reasoning Agent.**

Designed for users who prefer the server to handle LLM inference internally. This version includes the legacy inference layer and supports direct configuration of OpenAI and Anthropic API keys.

### ✨ Key Features (v0.2.9):
- **Internal Inference**: Can perform analysis and reviews directly using its own configured LLM provider.
- **All-in-One Logic**: Includes the legacy `inference/` module for direct model interactions.
- **Proven Stability**: The original stable version of the tool.

### 🛠 Installation
```bash
pipx install github-pr-context-mcp==0.2.9
```

---

## 🛠️ Configuration (v0.3.0)
The v0.3.0 version only requires a GitHub Token.

**Environment Variable:**
`GITHUB_TOKEN=ghp_your_token_here`

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
uvx github-pr-engine

# OR Install permanently
pipx install github-pr-engine
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
