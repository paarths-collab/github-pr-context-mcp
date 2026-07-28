# Quick Start and Usage

## Quickstart

### Prerequisites
- Python 3.10+
- An official v0.3 release with its product GitHub App configured. You will install and approve that App for only the repositories you want to index; you do not create an App or a token. See [Free local GitHub App flow](guides/github-app-device-flow.md).

> [!NOTE]
> This server follows a **Pure Context** model. You do **NOT** need to provide LLM API keys (Groq, OpenAI, etc.) to the server. Your IDE agent (Cursor, Claude, etc.) will use its own intelligence to process the data provided by this server.

---

## Installation & Setup

### Run the current v0.3 source checkout

**Using pipx:**
```bash
pipx install .
```

**Using uvx:**
```bash
uvx --from . github-pr-context-mcp
```

The package and command name are `github-pr-context-mcp`. If installing a published v0.3 release, use that same name with `uvx` or `pipx`.

---

## Usage

### 1. Connect GitHub once

Add the normal MCP command to your IDE, restart it, then ask the IDE agent to call:

1. `get_github_connection_status`
2. Open `app_installation_url` if it is returned, install the product App, and select only the repositories you want it to read.
3. `begin_github_authorization`
4. Open the returned GitHub URL, enter the one-time code, and approve the App.
5. `complete_github_authorization`

The access and refresh credentials stay in the operating-system credential vault. The MCP does not accept, return, or write a GitHub token to project files; it refreshes Device-Flow access automatically until GitHub requires a new approval.

### 2. Onboard a Repository
Simply ask your IDE agent:
`"Review this code using the history of owner/repo"`

The server will:
1. Check if `owner/repo` is indexed locally.
2. If not, it will ask if you want **permanent** (disk) or **temporary** (memory) storage.
3. It will fetch and index the PR history in the background.

### 3. Pure Context Tools
All history tools return raw JSON material. Your IDE agent reads that material, checks it against the current workspace, and performs the review or implementation itself.

**Example Tools:**
- `review_code_with_history`: Returns past review comments related to your code.
- `get_repo_rules_material`: Returns historical material for the agent to synthesize into local instructions.
- `generate_code_from_history`: Returns past implementation patterns for your task.

### 4. Example Prompts
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

The official release needs no GitHub environment variables in the IDE configuration. You may set `CHROMA_PERSIST_DIR` for a stable local index directory. Do not set an App client secret, private key, or `GITHUB_TOKEN` in the local configuration. See the [full setup and deployment boundary](guides/github-app-device-flow.md).

Fork maintainers can configure their own public App through `auth/product_github_app.py` before publishing their build. That is a maintainer task, not an end-user step.

---

Back to [README](../README.md)
