import argparse
import hashlib
import os
import platform
import sys
import threading


def _machine_fingerprint() -> str:
    """Generates a stable, anonymous machine fingerprint. No PII.
    Safe across Windows, macOS, Linux, and IDE-spawned processes.
    """
    parts = [platform.node(), platform.system(), platform.machine()]

    # os.getlogin() crashes in some IDE-spawned/non-TTY environments on all platforms
    for fn in (
        lambda: os.environ.get("USER") or os.environ.get("USERNAME") or "",
        lambda: str(os.getuid()) if hasattr(os, "getuid") else "",
    ):
        try:
            val = fn()
            if val:
                parts.append(val)
                break
        except Exception:
            pass

    raw = "-".join(p for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _send_startup_ping(mode: str) -> None:
    """Fire-and-forget anonymous ping to the Render server for user counting.
    Only sends if TELEMETRY_ENDPOINT is configured. Opt-out via TELEMETRY=false.
    Never blocks startup — runs in a daemon thread.
    """
    telemetry = os.getenv("TELEMETRY", "true").strip().lower()
    if telemetry in {"0", "false", "no", "off"}:
        return

    endpoint = os.getenv("TELEMETRY_ENDPOINT", "").strip()
    if not endpoint:
        return

    try:
        import requests  # always available — in pyproject.toml deps
        fingerprint = _machine_fingerprint()
        requests.post(
            f"{endpoint.rstrip('/')}/ping",
            json={"id": fingerprint, "mode": mode},
            timeout=3,
        )
    except Exception:
        pass  # Never surface telemetry errors to the user


def _check_for_updates() -> None:
    """Check if a newer version is available on GitHub and notify via stderr.
    This check is non-blocking and runs in a daemon thread.
    """
    try:
        from importlib.metadata import version
        import requests
        import re

        current_version = version("github-pr-context-mcp")
        # Check raw pyproject.toml on main branch for the latest version
        # This is faster and more reliable than the GitHub releases API for development versions
        url = "https://raw.githubusercontent.com/paarths-collab/github-pr-context-mcp/main/pyproject.toml"
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            match = re.search(r'version\s*=\s*"([^"]+)"', response.text)
            if match:
                latest_version = match.group(1)
                if latest_version != current_version:
                    print(
                        f"\n[UPDATE AVAILABLE] A new version of github-pr-context-mcp is available: {latest_version} (Current: {current_version})\n"
                        f"Run: pipx upgrade github-pr-context-mcp\n",
                        file=sys.stderr
                    )
    except Exception:
        pass  # Never block startup if network or version lookup fails


def _detect_mode() -> str:
    """Detect how this server was launched.

    Detection logic:
    - UV_PROJECT_ENVIRONMENT is set exclusively by uv/uvx virtual environments
    - PIPX_HOME or PIPX_LOCAL_VENVS are set by pipx
    - MCP_MODE can be set manually in the IDE env block for explicit override
    - Falls back to 'local' (git clone / direct python call)
    """
    # MCP_MODE explicit override takes precedence
    explicit = os.getenv("MCP_MODE", "").strip().lower()
    if explicit in {"uvx", "pipx", "local"}:
        return explicit

    # uv/uvx sets UV_PROJECT_ENVIRONMENT when running in a managed venv
    if os.getenv("UV_PROJECT_ENVIRONMENT"):
        return "uvx"

    # pipx sets PIPX_HOME when installing packages
    if os.getenv("PIPX_HOME") or os.getenv("PIPX_LOCAL_VENVS"):
        return "pipx"

    return "local"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitHub PR Context MCP Server - Provides historical PR review context for code reviews.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tools Overview:
  - ensure_repo_ready: Prepares a repository for querying (indexes PRs).
  - semantic_search_reviews: Search past review comments by meaning.
  - review_code_with_history: Get a code review based on past team patterns.
  - generate_code_from_history: Generate new code grounded in team history.
  - get_team_review_patterns: Identify recurring feedback in a repository.
  - list_indexed_repos: See which repositories are already available.

Configuration (Environment Variables):
  - GITHUB_TOKEN: (Required) Personal Access Token with 'repo' scope.
  - LLM_PROVIDER: (Optional) cerebras|openai|anthropic|gemini|ollama (default: cerebras).
  - LLM_API_KEY: (Optional) API key for your chosen provider.
  - CHROMA_PERSIST_DIR: (Optional) Custom path for persistent storage (default: ~/.github-pr-mcp/chroma_db).
  - TELEMETRY: (Optional) set to 'false' to opt-out of anonymous usage pings.

Important Concepts:
  - Permanent Storage: Indexed data is saved to disk and persists across restarts.
  - Temporary Storage: Indexed data is kept in memory and lost when the server stops.
  - Namespace: Use namespaces to isolate indexed data between different teams or users.

Example Usage (Claude Desktop Config):
{
  "mcpServers": {
    "github-pr-context": {
      "command": "github-pr-context-mcp",
      "env": {
        "GITHUB_TOKEN": "your_github_token_here",
        "LLM_PROVIDER": "anthropic",
        "LLM_API_KEY": "your_anthropic_key_here"
      }
    }
  }
}

Path & Installation:
  The executable is typically installed to your user's local bin directory.
  - Windows: %USERPROFILE%\\.local\\bin\\github-pr-context-mcp.exe
  - macOS/Linux: ~/.local/bin/github-pr-context-mcp
  
  If you are configuring Claude Desktop or another IDE, ensure you use the 
  ABSOLUTE PATH to the executable to avoid "command not found" errors.

Tool Selection & Strategy (When to use what):
  - Indexing: Always start with `ensure_repo_ready`. Use it again if the repo has changed significantly.
  - Research: Use `semantic_search_reviews` when you have a specific technical question (e.g., "How do we handle auth?").
  - Writing Code: Use `generate_code_from_history` for new features or refactors to stay consistent with team patterns.
  - Code Review: Use `review_code_with_history` before submitting a PR to catch issues early.
  - Analysis: Use `get_team_review_patterns` to understand the team's "soul" and recurring feedback themes.

Tool Selection Strategy (JSON for AI Agents):
  ```json
  {
    "strategy": {
      "onboarding": "ensure_repo_ready",
      "fact_finding": "semantic_search_reviews",
      "code_authoring": "generate_code_from_history",
      "pre_flight_check": "review_code_with_history",
      "culture_mapping": "get_team_review_patterns"
    },
    "decision_tree": {
      "if_new_repo": "ensure_repo_ready",
      "if_writing_code": "generate_code_from_history",
      "if_debugging": "semantic_search_reviews",
      "if_checking_standards": "get_team_review_patterns"
    }
  }
  ```

Troubleshooting:
  - "command not found": Use the absolute path. Run `github-pr-context-mcp config` to get it.
  - "invalid character": Fixed! This server now uses stderr for logs.
  - Rate limits: Ensure GITHUB_TOKEN is valid and has 'repo' scope.
  - Windows [WinError 32] (PermissionError):
      This happens when trying to 'pipx upgrade' while the server is running.
      1. Close MCP clients (Cursor, Claude Desktop).
      2. Run: taskkill /F /IM github-pr-context-mcp.exe
      3. Retry: pipx upgrade github-pr-context-mcp

Troubleshooting (JSON for AI Agents):
  ```json
  {
    "errors": {
      "WinError 32": {
        "cause": "Process lock. Binary is currently running/locked by Windows.",
        "remediation": [
          "taskkill /F /IM github-pr-context-mcp.exe",
          "Close IDEs (Cursor/Claude Desktop)",
          "Retry pipx upgrade"
        ]
      }
    }
  }
  ```
"""
    )
    parser.add_argument("command", nargs="?", choices=["config"], help="Run a helper command (e.g. 'config' to get your IDE snippet)")
    
    args = parser.parse_args()

    if args.command == "config":
        import json
        import sys
        
        # Detect absolute path of the current binary/script
        abs_path = os.path.abspath(sys.argv[0])
        command_val = abs_path
        
        # If running from source (.py file), prefix with python
        if abs_path.endswith(".py"):
            python_exe = sys.executable
            command_val = f"{python_exe} {abs_path}"
        
        detected_os = platform.system()
        
        config = {
            "mcpServers": {
                "github-pr-context": {
                    "command": command_val,
                    "env": {
                        "GITHUB_TOKEN": "YOUR_GITHUB_TOKEN",
                        "LLM_PROVIDER": "cerebras",
                        "LLM_API_KEY": "YOUR_LLM_API_KEY"
                    }
                }
            }
        }
        print(f"\n=== {detected_os.upper()} CONFIG SNIPPET ===", file=sys.stderr)
        print(f"Detected binary at: {command_val}", file=sys.stderr)
        print("Copy the JSON below into your mcpConfig.json file:", file=sys.stderr)
        print(json.dumps(config, indent=2))
        print("\nNOTE: Ensure you replace YOUR_GITHUB_TOKEN and YOUR_LLM_API_KEY.\n", file=sys.stderr)
        sys.exit(0)

    # Import here so that env vars from IDE env block are set before mcp_app loads
    from app.mcp_app import mcp

    mode = _detect_mode()
    # Send ping in background — startup is never delayed by telemetry
    threading.Thread(target=_send_startup_ping, args=(mode,), daemon=True).start()
    # Check for updates in background
    threading.Thread(target=_check_for_updates, daemon=True).start()
    
    # Run the server
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
