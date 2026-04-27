# GitHub PR Review Context MCP

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Protocol](https://img.shields.io/badge/Protocol-MCP-green)
![Data Source](https://img.shields.io/badge/Data-GitHub%20PR%20History-black?logo=github)
![Vector Store](https://img.shields.io/badge/Storage-ChromaDB-orange)
![Inference](https://img.shields.io/badge/LLM-Multi--Provider-brightgreen)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Status](https://img.shields.io/badge/Render%20Hosting-Upcoming-gray)
![Downloads](https://img.shields.io/badge/downloads-525-blue)
<!-- [![Users](https://img.shields.io/endpoint?url=https://your-app.onrender.com/usage/badge)](https://your-app.onrender.com/usage) -->

**Production-grade context layer for AI code review, grounded in your repository's real pull request history.**


> Tracking unique users across **uvx**, **pipx**, and **local** sources. (Render hosting upcoming)

</div>

---

## Overview

GitHub PR Review Context MCP gives AI assistants institutional review memory.

Instead of generic feedback, reviews are informed by historical reviewer comments, recurring quality patterns, and repository-specific standards from your own PR history.

### Core Value

- Improves review consistency across teams and repositories.
- Reduces repeated reviewer feedback on known issues.
- Integrates with any MCP-compatible client and multiple LLM providers.

---

## 🛠️ Usage Modes: Solo vs. Team

This MCP server is built to scale from a single machine to an entire engineering organization.

### 👤 Solo Developer (Local Mode)
**Best for:** Privacy, local-first control, and zero hosting costs.
- **How it works:** Run via `uvx`, `pipx`, or a local git clone.
- **Storage:** ChromaDB stays on your local machine.
- **Security:** Your GitHub Token and LLM keys never leave your device.
- **Setup:** See [Quick Start](docs/quickstart.md#🚀-zero-setup-uvx--pipx--npx).

### 🤝 Team Collaboration (Hosted Mode - UPCOMING)
**Best for:** Scaling team-wide PR standards and centralized infra.
- **How it works:** One deployment on Render (Coming Soon) shared by the whole team.
- **Isolation:** Strict **Gmail-based namespace isolation** (driven by SQLite). User A's indexed data is mathematically invisible to User B.
- **Economics:** Pooled LLM credits and a single shared indexing server.
- **Setup:** See [Deployment Guide](docs/integrations/deployed.md).

---

### 🌟 Zero-Friction Setup (Upcoming)
If your team has Hosted this MCP on Render, you do **NOT** need to `git clone` or install anything. You just drop a snippet into your IDE:

```json
"github-pr-context": {
  "type": "sse",
  "url": "https://YOUR-RENDER-URL.onrender.com/mcp",
  "headers": {
    "Authorization": "Bearer YOUR_TOKEN"
  }
}
```
*That's it.* If your IDE supports native MCP SSE connections, you are immediately connected to the secure Render deployment. No setup friction, no tools required.

---

## Key Capabilities

| Capability | What It Delivers |
|---|---|
| Historical review retrieval | Semantic search across prior PR comments and review summaries |
| Context-aware AI review | Feedback grounded in repository-specific review behavior |
| Grounded code generation | Generate new code based on past commits, comments, and style |
| **Team rules generation** | **Auto-generate .cursorrules / CLAUDE.md from repo history** |
| Smart repository readiness | Auto-detect indexed state and index on demand |
| Flexible storage modes | Permanent (disk) and temporary (in-memory) indexing options |
| Portable inference layer | Switch LLM providers using environment configuration only |

---

## Demo

![demo](assets/demo.gif)

Example workflow:
- Ask the assistant to review a diff using repository history.
- The server retrieves similar past review context.
- The model returns grounded feedback aligned to team expectations.

## Usage Analytics

To help us understand adoption, the MCP server collects privacy-first, anonymous telemetry on deployments. Future hosted deployments will expose HTTP endpoints (`/stats` and `/ping`) that publicly display the **number of unique users**.

---

## 🧰 Core Tools Reference

The server exposes 12 core tools for IDE agents and developers. For a deep dive on when to use each, see the [**Tool Strategy Guide**](docs/tools_strategy.md).

| Tool | Action |
|---|---|
| `ensure_repo_ready` | Index a repo and ensure it's ready for queries |
| `generate_repo_rules` | **Synthesize .cursorrules / CLAUDE.md from PR history** |
| `generate_code_from_history`| Write code grounded in past commits & team style |
| `review_code_with_history` | Perform AI review grounded in team review memory |
| `get_team_review_patterns` | Summarize recurring team standards (e.g. "no magic numbers") |
| `semantic_search_reviews` | Search past PR comments by meaning, not just keywords |
| `set_active_repo` | Switch between multiple indexed repositories |
| `list_indexed_repos` | View all repos currently in local/temporary storage |
| `delete_repo_index` | Free up disk space by clearing repository indices |
| `get_index_stats` | Verify if a repo index is complete (doc count) |
| `update_settings` | Update tokens/LLM keys (Hosted mode only) |
| `get_usage_stats` | View adoption metrics and unique user counts |

---

## Documentation

Detailed guides are split into focused pages:

- [Quick Start and Usage](docs/quickstart.md)
- [LLM Configuration](docs/llm-configuration.md)
- [Integrations](docs/integrations/index.md)
- [Architecture and Tools](docs/architecture.md)
- [Pipeline Deep Dive](docs/pipeline.md)
- [Configuration Guide (Change Tokens/Settings)](docs/guides/configuration.md)
- [Roadmap](docs/roadmap.md)

---

## Quick Links

- Access setup: [GitHub Token Guide](docs/GUIDE_GITHUB_TOKEN.md)
- Client connection: [Integrations](docs/integrations/index.md)

---

## 📣 Community & Feedback

We want to hear from you—whether you are a solo developer or a team at a large company!

### 👤 For Individuals
- **Feedback**: Please open an issue or start a discussion if you have ideas or encounter bugs.
- **Show your support**: If this tool saves you time, give it a **Star ⭐**! It helps others find the project.

### 🏢 For Corporate & Teams
- **Usage**: Is your team using this MCP server? Join our "Adopters" list by opening a PR to add your team's name.
- **Corporate Feedback**: Open an issue with the `corporate-usage` label to tell us how this has improved your PR review workflow.
- **Custom Integration**: Need help deploying this to your private cloud? Reach out via GitHub Discussions.

---

## 📜 Documentation & Guides

- **Strategy & Best Practices**: [Tool Strategy & Selection Guide](docs/tools_strategy.md)
- **Architecture**: [Architecture and Tools](docs/architecture.md)
- **Pipeline**: [Pipeline Deep Dive](docs/pipeline.md)
- **Usage**: [Quick Start and Usage](docs/quickstart.md)

## 🛠️ Troubleshooting

- **"command not found"**: Use absolute paths in your configuration. Run `github-pr-context-mcp config` to get your exact path.
- **"PermissionError: [WinError 32]"**: The binary is locked by a running process. Close Claude/Cursor, run `taskkill /F /IM github-pr-context-mcp.exe`, then retry the upgrade.
- **Rate Limit Errors**: Ensure your `GITHUB_TOKEN` is valid and has `repo` scope.

## ⚖️ License

MIT
