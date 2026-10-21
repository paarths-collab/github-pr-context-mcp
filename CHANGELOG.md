# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Index status states its real bounds.** `get_index_stats` now returns `bounds`
  (PRs indexed, oldest/newest PR number, oldest/newest `updatedAt`, configured
  PR limit and page count, `history_truncated`), `history_complete` and a
  one-sentence `coverage`. The bounds are stored in the cursor database and
  derived from the distinct PRs actually imported, so refresh overlap does not
  inflate them. Indexes built before this release report their bounds as
  unrecorded instead of guessing.
- **Evidence links on every retrieval hit.** Each hit carries
  `links.pr`, `links.review` and `links.comment` (null where GitHub has no such
  object, e.g. a PR description has no comment) plus `author_type`
  (`human` or `bot`). The GraphQL query now requests `url` and `__typename`.
- **`exclude_bots`** on every retrieval tool drops bot-authored hits.
- An end-to-end test drives the real stdio server with the official `mcp`
  client against replayed GitHub fixtures, including restart persistence
  (`tests/test_e2e_stdio.py`; see "Verified flow" in the README).

### Changed
- "Ready" no longer reads as "complete". The `ready` job message and the
  "already indexed" reply now say the job finished, not that the whole history
  was imported, and point at `coverage`.
- Bot detection trusts GitHub's actor type (`Bot`) over the login text, so a
  human whose login ends in `-bot` is no longer misclassified. Logins without a
  type (older payloads) fall back to the previous name heuristics.
- Inline comments by bots are still not indexed. Bot-authored PR descriptions
  and review summaries are, and are labelled `bot`.
- Commit messages inherit their PR author's type, because commit authors are not
  fetched.

### Fixed
- Clustering tests skip, with a reason, when the embedding model cannot be
  downloaded instead of erroring.

## [0.3.3] - 2026-07-29

### Fixed
- **The distributed LICENSE was the wrong project's.** It carried the upstream
  Model Context Protocol project's licensing-transition notice and the full
  Apache-2.0 text, opening with a statement about *that* project rather than
  this one, so GitHub reported this repository as Apache-2.0 while the README
  said MIT. The correct MIT licence was added in `f78e00c` and overwritten by
  the v0.3.0 release commit `6561a8e`; this restores that exact file. It is a
  correction, not a relicence. Artifacts published as 0.3.0, 0.3.1 and 0.3.2
  still contain the wrong file and cannot be changed.
- The package declared no licence at all, which is why PyPI showed none. It now
  ships PEP 639 metadata (`License-Expression: MIT`) and bundles the licence
  file, alongside audience and Python-version classifiers.

### Changed
- Build requires setuptools 77 or newer, for PEP 639 `license` / `license-files`.
  The older `license = {text = ...}` table form is deprecated and slated for
  removal.
- The README is a landing page rather than the whole manual: 417 words and two
  diagrams, down from 853 words and thirteen. The detail moved to the page that
  already owned each subject — retrieval/reasoning split, what gets indexed and
  the trust boundary to `docs/architecture.md`; the Device Flow handshake and
  connection state machine to `docs/guides/github-app-device-flow.md`; job
  states, page caps, webhook indexing, storage modes and limits to
  `docs/pipeline.md`; the tool surface to `docs/tools_strategy.md`.

### Removed
- The `benchmarks/` directory and the PR replay harness that produced it. The
  benchmark reported no statistically demonstrated effect on ten tasks, which is
  too little to conclude anything either way, so keeping the apparatus in the
  repository implied a claim the data did not support. `scripts/eval_harness.py`
  remains for retrieval-quality experiments.

## [0.3.2] - 2026-07-29

### Fixed
- **PR indexing worked in no published release.** `PR_QUERY` selected `diffHunk` on
  `PullRequestReviewThread`, a field GitHub defines only on
  `PullRequestReviewComment`. GitHub rejected the whole query with
  `undefinedField`, so every fetch failed before a single PR was indexed. Anyone
  running 0.3.0 or 0.3.1 could not index any repository. `flatten_pr` now reads
  the hunk from each comment, falling back to the first anchored hunk in the
  thread when a reply omits it, and `tests/test_queries_schema.py` guards the
  field's placement so the query text cannot silently drift from GitHub's schema
  again.
- Webhook indexing no longer advances the GitHub refresh watermark. A webhook
  delivers one PR rather than a complete newest-first sweep, so advancing the
  watermark let the next refresh skip anything updated earlier but not yet
  indexed.
- Webhook indexing drives an explicitly owned asyncio loop instead of the
  one-shot runner, which raised `RuntimeError` when the handler thread already
  had a running loop.
- Review-summary bot metadata no longer reads a variable left over from the
  inline-comment loop, which had tagged every summary with the last inline
  comment's bot flag.
- `python -m pytest` from the repository root no longer collects scratch helpers
  whose filenames match pytest's default `*_test.py` pattern.
- Legacy hosted GitHub token values are no longer returned through settings updates.
- A deleted index can no longer be recreated by a late background job.
- Capped refreshes no longer advance their GitHub watermark before all pages are
  processed.

### Added
- Optional standalone webhook listener
  ([`entrypoints/webhook_server.py`](entrypoints/webhook_server.py)) that indexes
  a PR into permanent storage as soon as it merges. It runs outside the
  local-stdio Device Flow path and reads its own `GITHUB_TOKEN`.
- Comment-quality filtering that keeps low-signal review chatter out of the
  embedding index.
- PR diff extraction, review clustering, and a retrieval eval harness.
- PR replay benchmark, which replayed already-merged PRs against their real
  merged diff and scored an agent with and without retrieved history. It found
  **no statistically demonstrated effect**: the measured gap was smaller than
  the judge's own disagreement between presentation orders. Removed after this
  release — see Unreleased. Its files exist in the `v0.3.2` tag.
- Clone-traffic export (`track_growth.py --export`) writing `metrics/clone-traffic.json`,
  preserving history past GitHub's rolling 14-day traffic window and naming its
  own gaps rather than presenting the series as continuous.
- Local GitHub App Device Flow with OS-vault-only credentials and token-free MCP tools.
- A v3 IDE skill that treats PR history as untrusted retrieval evidence and keeps reasoning in the connected IDE agent.
- Asynchronous index-job status, durable capped-refresh continuation, and deletion invalidation for in-flight jobs.
- Explicit `migrate-storage` support for copying pre-v3 local indexes and cursors into namespace-scoped v3 storage without deleting the source data.

### Changed
- The server is retrieval-only: it no longer presents server-side LLM generation or review as a product capability.
- PR extraction uses forward `updatedAt` pagination, stable GitHub node IDs, batch embeddings, and explicit truncation disclosure.
- Local namespaces use separate scoped collections; hosted GitHub retrieval remains disabled until a tenant-aware backend exists.
- Documentation is diagram-first: the README now carries the trust boundary,
  Device Flow handshake, connection and index-job state machines, page-cap
  arithmetic and tool surface as diagrams rather than prose.

### Note on versioning

This is numbered as a patch release, but it is the first release to contain the
v3 work: `v0.3.1` was tagged before that merge, so everything above ships here
for the first time. Replacing personal access tokens with GitHub App Device Flow
is a breaking change to how the server authenticates, and anyone upgrading from
0.3.0 or 0.3.1 must reconnect GitHub and run `migrate-storage`.

## [0.3.0] - 2024-05-08

### Added
- **Native Async Architecture**: Refactored the core engine to use `asyncio` and `httpx`, ensuring non-blocking operations for IDE agents.
- **CI/CD Awareness**: Automated detection of PRs touching CI files (`.github/workflows`, `Dockerfile`, `terraform`, etc.) via `touches_ci` metadata.
- **Error Pattern Matching**: New `find_similar_errors` tool for searching historical review discussions related to specific error messages or stack traces.
- **Stale Index Warnings**: Proactive notifications in tool responses when repository patterns are more than 30 days old.
- **Metadata Enrichment**: Added `is_bot` and `reviewer_login` to vector store records to provide better context on feedback origins.
- **Multi-Repo Workspace Support**: Enhanced repository auto-detection using `file_path` context from the IDE.

### Changed
- Migrated from `requests` to `httpx` for native asynchronous GitHub API communication.
- Wrapped all blocking storage (ChromaDB) and shell (Git) calls in `asyncio.to_thread`.
- Tool handlers now use `async def` to prevent event loop blocking.

### Fixed
- **Git Auto-Detection**: Implemented robust error handling for `subprocess` calls, preventing crashes in CI environments or non-git directories.
- **Protocol Stability**: Fixed various issues that caused stdout pollution and protocol-breaking log output.

## [0.2.10] - 2024-05-02

### Added
- **Incremental Indexing**: Support for fetching only new PRs since the last indexing run.
- **LRU Memory Management**: Automatic eviction of temporary (ephemeral) collections to prevent unbounded memory growth.

### Fixed
- Improved token window management by implementing strict truncation for large PR diffs and bodies.

## [0.1.0] - 2024-04-25

### Added
- Initial release with core semantic search and historical context retrieval.
- Support for permanent and temporary (in-memory) repository indexing.
- Multi-tenant safety via namespace isolation.
