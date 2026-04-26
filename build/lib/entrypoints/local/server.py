import hashlib
import os
import platform
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
    # Import here so that env vars from IDE env block are set before mcp_app loads
    from app.mcp_app import mcp

    mode = _detect_mode()
    # Send ping in background — startup is never delayed by telemetry
    threading.Thread(target=_send_startup_ping, args=(mode,), daemon=True).start()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
