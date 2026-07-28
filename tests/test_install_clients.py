"""Offline checks for token-free client configuration generation."""

import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_clients_under_test", PROJECT_ROOT / "scripts" / "install_clients.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_local_installer_prefers_the_isolated_installed_command(monkeypatch):
    installer = _load_installer()
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda command: "C:/Users/example/.local/bin/github-pr-context-mcp.exe",
    )

    config = installer.prompt_config()

    assert config == {
        "command": "C:/Users/example/.local/bin/github-pr-context-mcp.exe"
    }
