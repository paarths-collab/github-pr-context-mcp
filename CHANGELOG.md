# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Local GitHub App Device Flow with OS-vault-only credentials and token-free MCP tools.
- A v3 IDE skill that treats PR history as untrusted retrieval evidence and keeps reasoning in the connected IDE agent.
- Asynchronous index-job status, durable capped-refresh continuation, and deletion invalidation for in-flight jobs.
- Explicit `migrate-storage` support for copying pre-v3 local indexes and cursors into namespace-scoped v3 storage without deleting the source data.

### Changed
- The server is retrieval-only: it no longer presents server-side LLM generation or review as a product capability.
- PR extraction uses forward `updatedAt` pagination, stable GitHub node IDs, batch embeddings, and explicit truncation disclosure.
- Local namespaces use separate scoped collections; hosted GitHub retrieval remains disabled until a tenant-aware backend exists.

### Fixed
- Prevented legacy hosted GitHub token values from being returned through settings updates.
- Prevented a deleted index from being recreated by a late background job.
- Prevented capped refreshes from advancing their GitHub watermark before all pages are processed.

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
