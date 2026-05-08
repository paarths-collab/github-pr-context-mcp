# Roadmap

- [x] GraphQL fetching with review thread flattening
- [x] Local embeddings with ChromaDB persistence (permanent + temporary storage)
- [x] Pure Context Engine — offload reasoning to IDE agents
- [x] Auto-detect repo from git remote (zero-config startup)
- [x] File-path aware retrieval (multi-repo workspace support)
- [x] Non-blocking background indexing with `asyncio.to_thread`
- [x] Incremental indexing (fetch only new PRs since last index)
- [x] Shared-collection namespace isolation (metadata-level IDOR protection)
- [x] Temp collection LRU eviction (auto-cleanup of old memory indexes)
- [x] Input guards (truncation of massive diffs and bodies)
- [x] Robust GitHub API retries with `tenacity`
- [x] Structured logging to `stderr` (stdio health)
- [x] Session memory — per-repo context switching
- [x] Integrations: Antigravity, Claude Desktop, Claude Code, Cursor, Windsurf, GitHub Copilot, OpenCode
- [ ] Webhook-based auto-indexing
- [ ] Context pruning & high-density compression
- [ ] Multi-tenant usage dashboard

---

Back to [README](../README.md)
