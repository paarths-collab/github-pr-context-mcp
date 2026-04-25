import json
import os
import sys
from pathlib import Path

def get_appdata():
    if sys.platform == "win32":
        return os.environ.get("APPDATA", "")
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support")
    else:
        return os.path.expanduser("~/.config")

def get_home():
    return os.path.expanduser("~")

CLIENTS = {
    "Antigravity": {
        "path": os.path.join(get_appdata(), ".gemini", "antigravity", "mcp_config.json"),
        "key": "mcpServers"
    },
    "Claude Desktop": {
        "path": os.path.join(get_appdata(), "Claude", "claude_desktop_config.json"),
        "key": "mcpServers"
    },
    "Claude Code": {
        "path": os.path.join(get_home(), ".mcp.json"),
        "key": "mcpServers"
    },
    "Cursor": {
        "path": os.path.join(get_appdata(), "Cursor", "mcp.json"),
        "key": "mcpServers"
    },
    "Windsurf": {
        "path": os.path.join(get_appdata(), "Codeium", "windsurf", "mcp_config.json"),
        "key": "mcpServers"
    },
    "VS Code Copilot": {
        "path": os.path.join(get_home(), ".vscode", "mcp.json"),
        "key": "servers"
    },
    "OpenCode": {
        "path": os.path.join(get_appdata(), "opencode", "config.json"),
        "key": "mcp"
    }
}

def prompt_config():
    print("=== GitHub PR Context MCP Server Setup ===")
    print("\nHow are you running this MCP Server?")
    print("1. Locally (using uvx or python from this directory)")
    print("2. Hosted on Render (SSE / Remote)")
    
    choice = input("Select an option [1]: ").strip() or "1"
    
    mcp_host_config = {}
    
    if choice == "1":
        print("\nConfiguring Local execution...")
        github_token = input("GitHub PAT (leave blank to skip): ").strip()
        llm_provider = input("LLM Provider (default: cerebras): ").strip() or "cerebras"
        llm_api_key = input(f"LLM API Key for {llm_provider} (leave blank to skip): ").strip()
        
        # Determine execution path
        current_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        python_exe = sys.executable
        
        env_vars = {}
        if github_token: env_vars["GITHUB_TOKEN"] = github_token
        if llm_api_key: env_vars["LLM_API_KEY"] = llm_api_key
        if llm_provider: env_vars["LLM_PROVIDER"] = llm_provider
        env_vars["CHROMA_PERSIST_DIR"] = os.path.join(current_dir, "chroma_db")
        
        mcp_host_config = {
            "command": python_exe,
            "args": [os.path.join(current_dir, "entrypoints", "local", "server.py")],
            "env": env_vars
        }
    else:
        print("\nConfiguring Hosted Render execution (SSE bridge via npx)...")
        render_url = input("Render URL (e.g. https://github-pr-context-mcp.onrender.com): ").strip().rstrip("/")
        bearer_token = input("Auth Token (Bearer logic from GmailIdentity): ").strip()
        
        if not render_url:
            print("Error: Render URL is required.")
            sys.exit(1)
            
        sse_url = f"{render_url}/mcp/sse"
        
        # ── Fix: Most IDEs (Cursor, Windsurf, Claude Desktop) require generic stdio proxy wrappers for remote SSE endpoints.
        mcp_host_config = {
            "command": "npx",
            "args": [
                "-y",
                "@smithery/cli@latest",
                "run",
                sse_url,
                "--config",
                json.dumps({"headers": {"Authorization": f"Bearer {bearer_token}" if bearer_token else ""}})
            ]
        }

    return mcp_host_config

def inject_config(mcp_config, client_name, client_info):
    file_path = client_info["path"]
    key = client_info["key"]
    
    # Check if the parent directory exists
    dir_path = os.path.dirname(file_path)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True)
        
    config_data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    config_data = json.loads(content)
        except Exception as e:
            print(f"Warning: Could not parse {file_path}. Generating fresh config. ({e})")
            
    if key not in config_data:
        config_data[key] = {}
        
    config_data[key]["github-pr-context"] = mcp_config
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4)
        
    print(f"✅ Successfully injected config into {client_name} ({file_path})")


def main():
    mcp_config = prompt_config()
    
    print("\nWhich IDEs/Clients should we configure?")
    client_list = list(CLIENTS.items())
    for i, (name, _) in enumerate(client_list, 1):
        print(f"{i}. {name}")
    print("A. All of the above")
    
    selections = input("Select numbers (comma separated) or 'A' [A]: ").strip().upper() or "A"
    
    targets = []
    if selections == "A":
        targets = client_list
    else:
        for idx in selections.split(","):
            try:
                targets.append(client_list[int(idx.strip()) - 1])
            except (ValueError, IndexError):
                pass
                
    for name, info in targets:
        inject_config(mcp_config, name, info)
        
    print("\nDone! Feel free to restart your IDE or Client app to pick up the new MCP Server.")

if __name__ == "__main__":
    main()
