import argparse
import hashlib
import json
import os
import platform
import sys
import threading


# The local entrypoint is the only supported place for OS-vault Device Flow.
# Set this before importing app.mcp_app so the shared state module fails closed
# when it is instead loaded by an HTTP/deployment process.
os.environ.setdefault("GITHUB_PR_CONTEXT_RUNTIME", "local")


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
    Only sends when both TELEMETRY=true and TELEMETRY_ENDPOINT are configured.
    Never blocks startup — runs in a daemon thread.
    """
    telemetry = os.getenv("TELEMETRY", "false").strip().lower()
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
        description="GitHub PR Context v0.3 - Retrieves historical PR context for an IDE agent to reason over.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tools Overview:
  This is a pure-context server: it retrieves evidence; the IDE agent performs review,
  code generation, test design, and file edits.

  - get_github_connection_status / begin_github_authorization: Connect GitHub securely.
  - ensure_repo_ready: Prepares a repository for querying (indexes PRs).
  - semantic_search_reviews: Search past review comments by meaning.
  - review_code_with_history: Get historical material for an IDE code review.
  - generate_code_from_history: Get historical material for an IDE implementation.
  - get_team_review_patterns: Identify recurring feedback in a repository.
  - list_indexed_repos: See which repositories are already available.

Configuration (Environment Variables):
  - The official release already bundles its public GitHub App Client ID. Users do not add one.
  - GITHUB_APP_CLIENT_ID / GITHUB_APP_SLUG: Optional maintainer-only override for a fork or development build.
  - GITHUB_CREDENTIAL_PROFILE: (Optional) local OS-vault profile; defaults to 'default'.
  - CHROMA_PERSIST_DIR: (Optional) Custom path for persistent storage (default: ~/.github-pr-mcp/chroma_db).
  - TELEMETRY: (Optional) set to 'true' with TELEMETRY_ENDPOINT to enable anonymous usage pings.

Important Concepts:
  - Permanent Storage: Indexed data is saved to disk and persists across restarts.
  - Temporary Storage: Indexed data is kept in memory and lost when the server stops.
  - Namespace: Useful for local organization; do not treat hosted multi-user isolation as release-ready yet.

Example Usage (Claude Desktop Config):
{
  "mcpServers": {
    "github-pr-context": {
      "command": "github-pr-context-mcp"
    }
  }
}

After restarting the IDE, call get_github_connection_status, then
begin_github_authorization. Approve the one-time code in GitHub and call
complete_github_authorization. The credential is stored in the OS vault.
Users never paste a GitHub token or create their own GitHub App. Never put a
GitHub App client secret or private key in this local configuration.

Path & Installation:
  The executable is typically installed to your user's local bin directory.
  - Windows: %USERPROFILE%\\.local\\bin\\github-pr-context-mcp.exe
  - macOS/Linux: ~/.local/bin/github-pr-context-mcp
  
  If you are configuring Claude Desktop or another IDE, ensure you use the 
  ABSOLUTE PATH to the executable to avoid "command not found" errors.

Tool Selection & Strategy (When to use what):
  - Indexing: Always start with `ensure_repo_ready`. Use it again if the repo has changed significantly.
  - Research: Use `semantic_search_reviews` when you have a specific technical question (e.g., "How do we handle auth?").
  - Writing Code: Use `generate_code_from_history`, then let the IDE agent write and test the change.
  - Code Review: Use `review_code_with_history`, then let the IDE agent evaluate the evidence.
  - Analysis: Use `get_team_review_patterns` to understand the team's "soul" and recurring feedback themes.

Tool Selection Strategy (JSON for AI Agents):
  Load the block below at the START of every session. Match the user task to a trigger -> call that tool.
  Full reference: https://github.com/paarths-collab/github-pr-context-mcp/blob/main/docs/tools_strategy.md

  ```json
  {
    "tools": {
      "ensure_repo_ready":       { "call_when": "session start / new repo / repo changed" },
      "set_active_repo":         { "call_when": "user says switch/use a different repo" },
      "list_indexed_repos":      { "call_when": "user asks what repos are indexed" },
      "delete_repo_index":       { "call_when": "user wants to remove/reset index" },
      "semantic_search_reviews": { "call_when": "user asks technical question / wants past examples" },
      "review_code_with_history":{ "call_when": "user pastes code and asks for review" },
      "generate_code_from_history":{"call_when": "user asks to write/implement/generate code" },
      "get_team_review_patterns":{ "call_when": "user wants team norms / onboarding / standards" },
      "get_index_stats":         { "call_when": "verify index is complete / how many docs" },
      "get_repo_rules_material": { "call_when": "user wants historical material for local CLAUDE.md or .cursorrules" },
      "get_github_connection_status": { "call_when": "before first indexing / diagnose GitHub access" },
      "begin_github_authorization": { "call_when": "local GitHub is disconnected" },
      "complete_github_authorization": { "call_when": "after the user approves the one-time GitHub code" },
      "disconnect_github":       { "call_when": "user asks to remove local GitHub access" },
      "get_usage_stats":         { "call_when": "admin asks for adoption metrics" },
    },
    "session_flow": [
      "1. ensure_repo_ready",
      "2. get_team_review_patterns (optional)",
      "2b. get_repo_rules_material (optional — IDE agent writes any local rules file)",
      "3. semantic_search_reviews | generate_code_from_history | review_code_with_history",
      "4. get_index_stats (optional)"
    ]
  }
  ```

Troubleshooting:
  - "command not found": Use the absolute path. Run `github-pr-context-mcp config` to get it.
  - "invalid character": Fixed! This server now uses stderr for logs.
  - GitHub access: Check get_github_connection_status, then reconnect with Device Flow if needed.
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
    parser.add_argument(
        "command",
        nargs="?",
        choices=["config", "migrate-storage", "install-skill"],
        help="Run a helper command (e.g. 'config', 'migrate-storage', or 'install-skill')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report legacy-index migration work without writing data.",
    )
    parser.add_argument(
        "--skill-dir",
        help="Directory that should receive github-pr-context-v3 when using install-skill.",
    )
    
    args = parser.parse_args()

    if args.command == "config":
        # Detect absolute path of the current binary/script
        abs_path = os.path.abspath(sys.argv[0])
        
        # If running from source (.py file), use python and script path separately
        if abs_path.endswith(".py"):
            command = sys.executable
            args_list = [abs_path]
        else:
            command = abs_path
            args_list = []
        
        detected_os = platform.system()
        
        config = {
            "mcpServers": {
                "github-pr-context": {
                    "command": command,
                    "args": args_list
                }
            }
        }
        print(f"\n=== {detected_os.upper()} CONFIG SNIPPET ===", file=sys.stderr)
        print(f"Detected binary at: {abs_path}", file=sys.stderr)
        print("Copy the JSON below into your mcpConfig.json file:", file=sys.stderr)
        print(json.dumps(config, indent=2))
        print("\nNOTE: Restart the IDE, then use the GitHub Device Flow tools to connect. A configured official release includes its public App Client ID.\n", file=sys.stderr)
        sys.exit(0)

    if args.command == "migrate-storage":
        from storage.cursor_store import cursor_store
        from storage.legacy_migration import migrate_legacy_storage
        from storage.vector_store import _persistent_client

        report = migrate_legacy_storage(
            _persistent_client,
            cursor_store,
            dry_run=args.dry_run,
        )
        print(json.dumps(report, indent=2))
        sys.exit(0)

    if args.command == "install-skill":
        from pathlib import Path
        import shutil
        import sysconfig

        source_root = Path(__file__).resolve().parents[2] / ".agents" / "skills" / "github-pr-context-v3"
        if not source_root.is_dir():
            source_root = (
                Path(sysconfig.get_path("data"))
                / "share"
                / "github-pr-context-mcp"
                / "skills"
                / "github-pr-context-v3"
            )
        if not source_root.is_dir():
            raise RuntimeError("The packaged github-pr-context-v3 skill could not be found.")

        target_root = Path(args.skill_dir).expanduser() if args.skill_dir else Path.cwd() / ".agents" / "skills"
        destination = target_root / "github-pr-context-v3"
        if destination.exists():
            raise RuntimeError(
                f"Skill destination already exists: {destination}. Remove it deliberately before reinstalling."
            )
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, destination)
        print(f"Installed v3 skill at: {destination}")
        sys.exit(0)

    # Import here so that env vars from IDE env block are set before mcp_app loads
    from app.mcp_app import mcp

    mode = _detect_mode()
    # Send ping in background — startup is never delayed by telemetry
    threading.Thread(target=_send_startup_ping, args=(mode,), daemon=True).start()
    # Check for updates in background
    threading.Thread(target=_check_for_updates, daemon=True).start()
    
    # Run the server
    if mode == "local":
        print(
            "\n" + "="*60 + "\n"
            "[INFO] Running github-pr-context-mcp from a source checkout.\n"
            "="*60 + "\n"
            "To install this checkout into an isolated environment, run:\n"
            "  pipx install .\n\n"
            "The package command is github-pr-context-mcp.\n"
            "="*60 + "\n",
            file=sys.stderr
        )

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
