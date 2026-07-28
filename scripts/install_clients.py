import json
import os
import shutil
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
    
    # Check if running from a git clone
    if os.path.exists(".git"):
        print("\n[!] NOTICE: You are running this setup from a local git clone.")
        print("    For most users, we recommend: 'pipx install github-pr-context-mcp'")
        print("    This automatically handles absolute paths and simplifies updates.\n")

    print("How are you running this MCP Server?")
    print("1. Local v3 (recommended, free, and private to this machine)")
    print("2. Remote hosted deployment (not available for public self-service in v0.3)")
    
    choice = input("Select an option [1]: ").strip() or "1"
    
    mcp_host_config = {}
    
    if choice == "1":
        print("\nConfiguring Local execution...")
        print("A configured v0.3 release uses the bundled product GitHub App through Device Flow.")
        print("Do not paste a GitHub token or Client ID. After your IDE restarts, the")
        print("MCP will give you the safe GitHub installation and approval links.")

        # Prefer the actual installed entrypoint. ``pipx install .`` creates an
        # isolated virtual environment, so using this script's interpreter would
        # otherwise make the IDE miss the MCP package and its dependencies.
        installed_command = shutil.which("github-pr-context-mcp")
        current_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if installed_command:
            print(f"Using installed command: {installed_command}")
            mcp_host_config = {"command": installed_command}
        else:
            print("Installed command not found; using this source checkout's Python environment.")
            print("Install with 'pipx install .' for a portable, isolated command.")
            mcp_host_config = {
                "command": sys.executable,
                "args": [os.path.join(current_dir, "entrypoints", "local", "server.py")],
                "env": {
                    "CHROMA_PERSIST_DIR": os.path.join(current_dir, "chroma_db")
                },
            }
    else:
        print("\nRemote public onboarding is intentionally disabled in v0.3.")
        print("It needs tenant-aware authentication, encrypted server-side storage, and")
        print("a product-owned GitHub App backend. Use the free local option instead.")
        sys.exit(2)

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
        
    print("\nDone! Restart your IDE or client, then call get_github_connection_status.")
    print("If it is disconnected, call begin_github_authorization, install the App on")
    print("only the repositories you choose, and complete the one-time GitHub approval.")

if __name__ == "__main__":
    main()
