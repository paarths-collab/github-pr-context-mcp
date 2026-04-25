# GitHub Personal Access Token (PAT) Guide

To index and review repositories, this MCP server needs a GitHub Personal Access Token. Follow this guide to create one with the minimum required permissions.

---

## Option 1: Fine-grained Token (Recommended 🔒)

Fine-grained tokens are more secure because you can limit them to specific repositories and set very specific permissions.

1.  Go to [GitHub Settings > Personal Access Tokens > Fine-grained tokens](https://github.com/settings/tokens?type=beta).
2.  Click **Generate new token**.
3.  **Token Name**: `PR-Review-MCP` (or anything you like).
4.  **Expiration**: Select a timeframe (e.g., 90 days).
5.  **Repository access**:
    *   Select **Only select repositories** and pick the repos you want to index.
    *   *Alternatively*, select **All repositories** if you plan to index many projects.
6.  **Repository permissions**:
    Select the **Read-only** access level for the following scopes:

    | Permission | Access | Why? |
    | :--- | :--- | :--- |
    | **Metadata** | `Read-only` | Required to identify the repository and its owner. |
    | **Pull requests** | `Read-only` | Required to fetch PR titles, bodies, and review comments. |
    | **Contents** | `Read-only` | Required to fetch file paths and the changes within PRs. |

7.  Click **Generate token** and copy it immediately.

---

## Option 2: Classic Token (Legacy 🛠️)

Use this if you prefer the old system or need to access organizations that don't yet support fine-grained tokens.

1.  Go to [GitHub Settings > Personal Access Tokens > Tokens (classic)](https://github.com/settings/tokens).
2.  Click **Generate new token > Generate new token (classic)**.
3.  **Note**: `PR-Review-MCP`.
4.  **Select scopes**:
    *   `repo` (REQUIRED: Check this to access private and public repositories).
    *   *Or* just `public_repo` if you only plan to index open-source projects.
5.  Click **Generate token** and copy it immediately.


---

## Where to add the token?

Once you have your token, add it to your `.env` file or your MCP client configuration (like Antigravity or Claude Desktop):

### In your `.env` file:
```bash
GITHUB_TOKEN=github_pat_11A...
```

### In your MCP Client Config:
```json
"env": {
  "GITHUB_TOKEN": "github_pat_11A..."
}
```

> [!CAUTION]
> **Never commit your token to a public repository.** Ensure your `.env` file is listed in `.gitignore` (we've handled this for you by default).

---

Back to [README](../README.md)
