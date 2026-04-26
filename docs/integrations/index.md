# Integrations

This project supports two clean deployment modes:

- Local MCP: the server runs on the user's machine, so Chroma storage stays local to that user.
- Deployed MCP (Upcoming): the server runs on Render, and storage is isolated by namespace or user identity.

If you want each user's storage to remain local to their machine, use the local mode.
If you want a shared hosted service (Upcoming), use the deployed mode plus Gmail-only auth.

## Quick Compare

| Mode | User Type | Where Storage Lives | Key Benefit |
|---|---|---|---|
| **Solo Developer** | Individual | Local machine | Full privacy & control |
| **Team Collaboration**| Teams / Orgs | Render (Upcoming) | Shared standards & infra |

## 🕹️ Choosing your mode

### 1. Solo Developer (Local)
Recommended if you are an individual developer or working on highly sensitive local code. The server runs as a child process of your IDE via `uvx`, `pipx`, or a local installation.

- [Local Setup Guide](local.md)

### 2. Team Collaboration (Hosted - UPCOMING)
Recommended for engineering teams that want a single "Review Source of Truth." One person deploys to Render, and the rest of the team connects via a secure Bearer token. 

- [Deployment Guide](deployed.md)
- [Gmail-Only Auth Flow](auth.md)
