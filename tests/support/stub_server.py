"""Run the real stdio MCP server with GitHub and the embedding model stubbed.

Launched as a subprocess by the end-to-end test (``python stub_server.py``). It
imports and runs the production ``entrypoints.local.server.main`` unchanged; only
three seams are replaced before it starts:

* ``fetcher.client.run_query``  -> replays a JSON fixture (no network, no token)
* the GitHub credential provider -> returns a fixed dummy value (no OS vault)
* the sentence-transformers model -> a deterministic hashing embedder

Environment:
  GITHUB_REPLAY_FIXTURE   fixture file name inside tests/fixtures/github
  GITHUB_REPLAY_LOG       optional path; each replayed request is appended to it
  CHROMA_PERSIST_DIR, CURSOR_DB_PATH   where the index lives (set by the caller)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github_replay  # noqa: E402
from hashing_model import HashingModel  # noqa: E402


def _install_stubs() -> None:
    import fetcher.client
    import storage.encoder
    import app.tools.indexing
    import entrypoints.local.server as server

    fixture = github_replay.load_fixture(
        os.environ.get("GITHUB_REPLAY_FIXTURE", "fixture-owner__fixture-repo.json")
    )
    fetcher.client.run_query = github_replay.make_run_query(
        fixture, os.environ.get("GITHUB_REPLAY_LOG")
    )

    async def stub_token() -> str:
        return github_replay.REPLAY_TOKEN

    app.tools.indexing.get_github_access_token = stub_token
    storage.encoder._model = HashingModel()
    # Keep the run hermetic: the real entrypoint pings raw.githubusercontent.com.
    server._check_for_updates = lambda: None


if __name__ == "__main__":
    _install_stubs()
    from entrypoints.local.server import main

    main()
