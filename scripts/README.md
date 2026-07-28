# Client Setup Scripts

To configure your IDE/client for the local v0.3 MCP server:

```bash
python scripts/install_clients.py
```

For the recommended local v0.3 setup, this configures only the MCP command and local index path. A configured official release contains the product GitHub App's public Client ID. After restarting the IDE, use `get_github_connection_status` and `begin_github_authorization` to install/approve the App; it never asks you to paste a GitHub token, Client ID, App secret, or private key.

Public and controlled hosted onboarding are intentionally disabled in v0.3. A hosted service needs tenant-aware GitHub App authentication and encrypted per-user storage; use the local option until that separate deployment is implemented.

It can configure:
1. Antigravity (`mcp_config.json`)
2. Claude Desktop
3. Claude Code
4. Cursor (`mcp.json`)
5. Windsurf
6. VS Code Copilot
7. OpenCode

No copy-pasting JSON required!
