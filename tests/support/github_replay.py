"""Replay recorded GitHub GraphQL responses in place of the live API.

``fetcher.client.run_query`` is the only place the indexer talks to GitHub.
``make_run_query`` returns a drop-in replacement that serves the pages stored in
a JSON fixture, so tests and the stdio verification run need no network and no
GitHub credential.

Fixture shape::

    {"repo": "owner/name",
     "pages": [{"request": {"cursor": null}, "response": {...GraphQL JSON...}}, ...]}

A request that matches no recorded page fails loudly instead of returning an
empty result, so a change in how pages are requested cannot pass silently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Not a credential: the stub provider hands this out and the replay checks it,
# which proves the indexer went through the credential provider.
REPLAY_TOKEN = "replay-token-not-a-real-credential"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "github"


class ReplayError(AssertionError):
    """The indexer asked for something the fixture does not contain."""


def load_fixture(name: str = "fixture-owner__fixture-repo.json") -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def make_run_query(fixture: dict, log_path: str | None = None):
    """Build an async ``run_query`` that replays ``fixture``.

    Each served request is appended to ``log_path`` (JSON lines) when given, so
    a test can assert how many pages the server really fetched.
    """
    from fetcher.queries import PR_QUERY

    pages = {
        (page["request"].get("cursor")): page["response"] for page in fixture["pages"]
    }
    repo = fixture["repo"]

    async def run_query(query: str, variables: dict, github_token: str | None = None) -> dict:
        if query != PR_QUERY:
            raise ReplayError("Indexer sent a query other than fetcher.queries.PR_QUERY.")
        if github_token != REPLAY_TOKEN:
            raise ReplayError("Indexer did not use the credential provider's token.")
        requested = f"{variables.get('owner')}/{variables.get('repo')}"
        if requested != repo:
            raise ReplayError(f"No fixture for {requested}; only {repo} is recorded.")
        cursor = variables.get("cursor")
        if cursor not in pages:
            raise ReplayError(f"No recorded page for cursor {cursor!r}.")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(json.dumps({"repo": requested, "cursor": cursor}) + "\n")
        return json.loads(json.dumps(pages[cursor]))  # callers may mutate it

    return run_query


def read_request_log(log_path: str | None) -> list[dict]:
    if not log_path or not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as log:
        return [json.loads(line) for line in log if line.strip()]
