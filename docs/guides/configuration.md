This guide explains how to change your GitHub tokens, LLM providers, and API keys depending on how you are running the MCP server.

## 🪄 The Easiest Way: Just Ask!
If you are using an AI agent (like Cursor, Claude Desktop, or Windsurf), the fastest way to change your settings is to **just tell the agent**. 

**Example Prompts:**
*   *"Agent, change my GitHub token to ghp_xyz123..."*
*   *"Switch my LLM provider to Groq and use llama-3.3-70b-versatile"*
*   *"Update my settings to use my personal OpenAI API key"*

The Agent will use the `update_settings` tool to handle the configuration for you automatically.

---

## 🏗️ 1. Local Mode (uvx / npx / pipx / git clone)

When running locally, your settings are controlled by **Environment Variables**. To change them, you must update the configuration in your IDE (Cursor, VS Code, etc.).

### How to change:
1. **Find your IDE config**:
   - **Claude Desktop**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **Cursor/VS Code**: Go to **Settings** -> **MCP** -> **Edit Server**.
2. **Update the `env` block**: 
   Look for the `env` section and swap your keys:
   ```json
   "env": {
     "GITHUB_TOKEN": "ghp_NEW_TOKEN",
     "LLM_PROVIDER": "anthropic",
     "ANTHROPIC_API_KEY": "sk-ant-..."
   }
   ```
3. **Restart**: You must restart the MCP server in your IDE for variables to take effect.

---

## 🤝 2. Team Mode (Render / Deployed)

In a shared Team deployment, there are two ways to handle configuration: **Global** vs. **Personal**.

### A. Global Configuration (Admin only)
If you want the whole team to use the same keys by default, update the "Environment Variables" in the **Render Dashboard**.
- Changes take effect for everyone who hasn't set their own personal keys.

### B. Personal Overrides (Per-User)
Authenticated users can store their **own** keys inside the server. These keys stay locked to your Gmail identity and override the global ones.

- **Check current settings**: `GET /settings` (requires Bearer token).
- **Update settings**: `PUT /settings` with a JSON payload:
  ```json
  {
    "settings": {
      "github_token": "ghp_your_personal_token",
      "llm_provider": "openai",
      "llm_api_key": "sk-..."
    }
  }
  ```

---

## 🧪 Summary of Installation vs. Configuration

| Tool | Type | Purpose |
|---|---|---|
| **uvx / pipx / npx** | **Installation** | Downloads the code and makes the server available on your machine. |
| **install_clients.py** | **Configuration** | Updates your `claude_desktop_config.json` or IDE settings with the correct paths. |

> **Note on Tracking**: Both methods are tracked. `uvx` users are counted separately from people who manually configured their environment via `install_clients.py`.

---

Back to [Quickstart](../quickstart.md)
