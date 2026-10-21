# v0.3 Roadmap

## Current direction

- [x] Retrieve GitHub pull-request and review history.
- [x] Build local semantic-search indexes with ChromaDB.
- [x] Return historical context to IDE agents instead of calling a server-side reasoning model.
- [x] Provide a repository-local v3 agent skill for tool selection and retrieval-first workflows.
- [x] Add local GitHub App Device Flow with operating-system-vault credential storage.
- [x] Refresh Device-Flow GitHub access locally without shipping a client secret.

## Implemented (local, tested with stubbed GitHub)

- [x] Background indexing jobs, with status exposed through `get_index_stats`.
- [x] Incremental refresh (`ensure_repo_ready(refresh=true)`) with persisted checkpoints.
- [x] Index generations with atomic publish, so a failed run keeps the previous generation serving.
- [x] Persisted coverage and index states (`no_index`, `indexing`, `failed`, `ready_partial`, `ready_selected_scope`, `stale`).
- [x] Resumable older-history backfill via `ensure_repo_ready(backfill=true)`, which works after a restart.
- [x] Evidence-unit results with source links and human/bot provenance (`get_evidence` expands a unit to its full thread).
- [x] Token-budget fitting (`budget_tokens`): whole units are kept or omitted, never truncated.
- [x] Candidate-conflict annotation: possible reversals are flagged heuristically, without deciding which side is current.
- [x] Lexical degraded mode when the embedding model cannot be loaded (`retrieval_mode == "lexical_degraded"` with a warning).
- [x] Stdio end-to-end test that drives the MCP server with GitHub stubbed.
- [ ] Webhook code exists (`entrypoints/webhook_server.py`, with unit tests), but live GitHub webhook delivery has not been verified.

## Required before claiming a stronger release

- [ ] Test every MCP retrieval tool through its registered interface.
- [ ] Run the live GitHub App authorization flow end to end against a real installation.
- [ ] Verify GitHub pagination and incremental-update behavior against representative repositories.
- [ ] Extend nested pagination of files, commits and reviews beyond the current bounds (bounds are disclosed in results).
- [ ] Measure retrieval relevance with a benchmark before claiming retrieval quality.
- [ ] Add tenant-specific indexing, querying, listing, and deletion tests before supporting multi-user hosting.
- [ ] Complete and test a hosted registration and token-lifecycle flow.
- [ ] Support hosted multi-user retrieval.
- [ ] Index open PRs and issue comments.
- [ ] Test package installation and MCP startup in CI.

## Later improvements

- [ ] Webhook-driven reindexing verified against live GitHub deliveries.
- [ ] Design a separate hosted tenant credential vault/KMS; do not reuse the local OS-vault flow.
- [ ] More explicit freshness information in retrieval responses (per-result freshness; `index_state` and `stale_reason` are already exposed).
- [ ] Chunking and reranking quality improvements.
- [ ] Context pruning tuned by measured retrieval quality.

Back to [README](../README.md)
