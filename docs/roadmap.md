# v0.3 Roadmap

## Current direction

- [x] Retrieve GitHub pull-request and review history.
- [x] Build local semantic-search indexes with ChromaDB.
- [x] Return historical context to IDE agents instead of calling a server-side reasoning model.
- [x] Provide a repository-local v3 agent skill for tool selection and retrieval-first workflows.
- [x] Add local GitHub App Device Flow with operating-system-vault credential storage.
- [x] Refresh Device-Flow GitHub access locally without shipping a client secret.

## Required before claiming a stronger release

- [ ] Test every MCP retrieval tool through its registered interface.
- [ ] Make indexing outcomes visible: queued, running, ready, or failed.
- [ ] Add a real refresh flow for existing indexes.
- [ ] Add tenant-specific indexing, querying, listing, and deletion tests before supporting multi-user hosting.
- [ ] Complete and test a hosted registration and token-lifecycle flow.
- [ ] Verify GitHub pagination and incremental-update behavior against representative repositories.
- [ ] Test package installation and MCP startup in CI.

## Later improvements

- [ ] Webhook-driven reindexing.
- [ ] Design a separate hosted tenant credential vault/KMS; do not reuse the local OS-vault flow.
- [ ] More explicit freshness information in retrieval responses.
- [ ] Context pruning tuned by measured retrieval quality.

Back to [README](../README.md)
