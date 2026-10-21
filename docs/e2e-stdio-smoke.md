# Stdio end-to-end smoke test

Proves the published artifact works the way an IDE uses it: **build the wheel, install it into a
fresh venv, launch the installed `github-pr-context-mcp` as a stdio server, talk to it with the
official `mcp` Python client, kill it, start a second process on the same HOME.**

## Run it

```bash
# Full fresh install + transcript (always does `uv build` -> new Python 3.11 venv -> wheel install)
.venv/bin/python scripts/e2e_stdio_smoke.py [--transcript out.json] [--keep] [--encoder auto|real|fake]

# The same flow as a pytest test (marker: e2e)
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest tests/e2e -q -s

# Skip it
E2E_SKIP=1 .venv/bin/python -m pytest -q          # or: -m "not e2e"
```

Needs `uv`, `git`, network access to PyPI on a cold uv cache (torch is large; a warm cache installs in
seconds) and the `mcp` package in the interpreter that runs the script. The test skips itself when `uv`,
`git`, a uv-visible Python 3.11 or the `mcp` client is missing. Optional switches: `E2E_VENV=/path`
(reuse a venv that already has the wheel; pytest only), `E2E_ENCODER=real|fake|auto`,
`E2E_TRANSCRIPT=/path.json`.

## What is isolated

| Concern | How |
| --- | --- |
| Code under test | the **installed** wheel (the harness checks `app`/`storage` import from `site-packages`, not the repo) |
| Files | `HOME`, `CHROMA_PERSIST_DIR`, `CURSOR_DB_PATH` and the cwd (no `.env`) are in a temp dir |
| Credentials | `PYTHON_KEYRING_BACKEND=e2e_keyring.FileKeyring` (JSON file in the temp dir, so the credential survives a restart; no OS keychain); no `GITHUB_TOKEN`; dummy `GITHUB_APP_CLIENT_ID`; `TELEMETRY` unset |
| Network | `tests/e2e/sitecustomize.py` (on `PYTHONPATH` of the server only) puts an `httpx.MockTransport` behind every `httpx` client and refuses `requests` (the startup update check). It answers Device Flow, `GET /user` and GraphQL from `tests/e2e/e2e_fixture.py` (invented `demo-org/demo-repo`, 6 closed PRs, 2 PRs per stub page) |
| Embeddings | the real `all-MiniLM-L6-v2` from `~/.cache/huggingface` with `HF_HUB_OFFLINE=1`; falls back to a deterministic hashing encoder (reported as FAKE) when the model is not cached |

Revoked auth is simulated by creating a flag file; while it exists the stub answers GraphQL with 401.

## What the flow checks

1. `list_tools` exposes the key tools (incl. `get_evidence`); GitHub starts disconnected; indexing before
   login fails with `authorization_required` and search returns an actionable empty envelope.
2. Device Flow: `begin_github_authorization` -> `complete_github_authorization` (pending once, then
   connected); no token or device code appears in any tool output.
3. `ensure_repo_ready(pages=1)`: bounded window (`#105-#106`), `older_history_remaining`, state
   `ready_selected_scope`.
4. `semantic_search_reviews`: unit/coverage/budget shape, human **and** bot threads plus a bot review
   summary, `exclude_bots`, `source_kinds`, `budget_tokens=256`, `candidate_conflicts` for the retry
   reversal (`#105` vs `#106`), prompt-injection text only inside an excerpt.
5. `get_evidence` returns every part of the 53-comment thread (the last comments come from a follow-up
   `node(id:)` query).
6. A `temporary` index; `backfill=true` makes a previously unreachable PR retrievable (`ready_partial`).
7. Refresh, then revoked auth: `index_state=stale`, `error_kind=authorization_revoked`, search still serves
   the previous generation with a warning.
8. `kill -9`, new process, same HOME: credential connected, permanent index/coverage/failure outcome
   persisted, backfill cursor `c:4` in SQLite and **used** (first GraphQL request after restart), the
   temporary index is gone, final backfill completes the history.

The script prints a timed transcript and writes a JSON transcript (default outside the repo).
