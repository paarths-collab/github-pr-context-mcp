"""End-to-end test: fresh wheel install + a real MCP stdio client session + restart.

Marked ``e2e`` and OPT-IN: a bare ``pytest`` skips it because it costs ~2.5 minutes
and downloads torch on a cold cache. Run it with ``E2E_RUN=1 pytest tests/e2e`` or by
selecting the marker (``pytest -m e2e``, which needs no ``E2E_RUN``). It builds the wheel, installs it into a brand-new Python 3.11 venv
with uv, launches the INSTALLED ``github-pr-context-mcp`` console script and drives
it with the official ``mcp`` client (see tests/e2e/e2e_harness.py for the flow and
tests/e2e/sitecustomize.py for the in-process GitHub stub). Takes 1-2 minutes with a
warm uv cache (first run downloads torch from PyPI).

``E2E_SKIP=1`` always wins and skips it, as does ``-m "not e2e"``; it also skips itself when uv, git,
Python 3.11 (via uv) or the ``mcp`` client are unavailable. Set ``E2E_VENV=/path``
to reuse a venv that already has the wheel installed, ``E2E_ENCODER=fake|real`` to
force the encoder, ``E2E_TRANSCRIPT=/path.json`` to keep the JSON transcript.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_harness as h  # noqa: E402

pytestmark = pytest.mark.e2e


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _marker_selected(config) -> bool:
    """True when ``-m`` explicitly selects e2e (``-m e2e``, not ``-m "not e2e"``)."""
    expr = (getattr(config.option, "markexpr", "") or "").lower()
    return re.search(r"(?<!not )(?<![\w])e2e(?![\w])", expr) is not None


def _skip_reason(config=None) -> str | None:
    if _truthy("E2E_SKIP"):
        return "E2E_SKIP is set"
    if not _truthy("E2E_RUN") and not (config is not None and _marker_selected(config)):
        return "e2e is opt-in: set E2E_RUN=1 or select it with `pytest -m e2e`"
    if shutil.which("uv") is None:
        return "uv is not installed"
    if shutil.which("git") is None:
        return "git is not installed"
    if importlib.util.find_spec("mcp.client.stdio") is None:
        return "the mcp client package is not importable"
    if not os.environ.get("E2E_VENV"):
        found = subprocess.run(["uv", "python", "find", "3.11"], capture_output=True, text=True)
        if found.returncode != 0:
            return "uv cannot find a Python 3.11 interpreter (offline?)"
    return None


@pytest.fixture(scope="module")
def e2e_run(tmp_path_factory, request):
    reason = _skip_reason(request.config)
    if reason:
        pytest.skip(reason)
    workdir = tmp_path_factory.mktemp("pr-context-e2e")
    rec = h.Recorder(verbose=True)
    outcome = {"rec": rec, "error": None, "workdir": workdir}
    try:
        h.run_all(workdir, rec)
    except Exception as exc:  # noqa: BLE001 - surfaced by the tests below
        outcome["error"] = exc
        log = workdir / "sandbox" / "state" / "server-stderr.log"
        if log.exists():
            print("--- server stderr (tail) ---")
            print("\n".join(log.read_text(errors="replace").splitlines()[-25:]))
    finally:
        rec.save(os.environ.get("E2E_TRANSCRIPT") or workdir / "e2e-transcript.json")
    return outcome


def test_fresh_install_runs_the_installed_package(e2e_run):
    install = e2e_run["rec"].facts.get("install")
    assert install, f"fresh install did not complete: {e2e_run['error']!r}"
    assert "site-packages" in install["app_file"] and "site-packages" in install["storage_file"]
    assert not install["app_file"].startswith(str(h.ROOT)), "imports must come from the venv, not repo sources"
    assert install["version"]
    assert Path(install["script"]).exists()


def test_stdio_flow_with_restart(e2e_run):
    rec = e2e_run["rec"]
    if e2e_run["error"] is not None:
        raise e2e_run["error"]
    assert not rec.failures
    assert rec.checks >= 100
    assert rec.facts["tool_count"] >= 20
    assert rec.facts["encoder"]
