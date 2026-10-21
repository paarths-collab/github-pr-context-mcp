#!/usr/bin/env python
"""Runnable stdio smoke test: fresh install + a real MCP client session + restart.

    .venv/bin/python scripts/e2e_stdio_smoke.py [--transcript PATH] [--keep]

Always does the FULL fresh install (``uv build`` -> new Python 3.11 venv -> wheel
install), launches the installed ``github-pr-context-mcp`` console script as a
stdio server with a hermetic environment, drives it with the official ``mcp``
client, SIGKILLs it, starts a second process on the same HOME and checks what
persisted. GitHub is stubbed inside the server process by
``tests/e2e/sitecustomize.py``; all data is invented. Prints a transcript with
timings and writes a JSON transcript outside the repo. See docs/e2e-stdio-smoke.md.

This script always runs (it needs no flag). The pytest wrapper of the same flow,
tests/e2e/test_e2e_stdio.py, is opt-in: it is skipped by a bare ``pytest`` unless
``E2E_RUN=1`` is set or the marker is selected with ``pytest -m e2e``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

DEFAULT_TRANSCRIPT = str(Path(tempfile.gettempdir()) / "e2e-transcript.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--transcript", default=DEFAULT_TRANSCRIPT, help="where to write the JSON transcript (keep it outside the repo)")
    parser.add_argument("--keep", action="store_true", help="keep the temp directory (venv, state, server log)")
    parser.add_argument("--encoder", choices=["auto", "real", "fake"], default=None, help="override E2E_ENCODER")
    args = parser.parse_args()

    import os

    if args.encoder:
        os.environ["E2E_ENCODER"] = args.encoder
    if shutil.which("uv") is None:
        print("uv is required (https://docs.astral.sh/uv/).", file=sys.stderr)
        return 2
    os.environ.pop("E2E_VENV", None)  # the script always does the full fresh install

    import e2e_harness as h

    workdir = Path(tempfile.mkdtemp(prefix="pr-context-e2e-"))
    rec = h.Recorder(verbose=True)
    rec.say(f"workdir {workdir}")
    ok = False
    try:
        h.run_all(workdir, rec)
        ok = True
    except Exception as exc:  # noqa: BLE001 - report and keep the evidence
        rec.say(f"FAILED: {type(exc).__name__}: {exc}")
        log = workdir / "sandbox" / "state" / "server-stderr.log"
        if log.exists():
            print("--- server stderr (tail) ---\n" + "\n".join(log.read_text(errors="replace").splitlines()[-25:]))
    finally:
        rec.save(args.transcript)
        print(f"\ntranscript: {args.transcript}")
        print("timings (s):", rec.timings)
        print(f"checks passed: {rec.checks - len(rec.failures)}/{rec.checks}  failures: {rec.failures}")
        if args.keep or not ok:
            print(f"kept {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
