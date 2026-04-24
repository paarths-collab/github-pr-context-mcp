# GitHub PR Review Context MCP

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Protocol](https://img.shields.io/badge/Protocol-MCP-green)
![Data Source](https://img.shields.io/badge/Data-GitHub%20PR%20History-black?logo=github)
![Vector Store](https://img.shields.io/badge/Storage-ChromaDB-orange)
![Inference](https://img.shields.io/badge/LLM-Multi--Provider-brightgreen)

**Production-grade context layer for AI code review, grounded in your repository's real pull request history.**

</div>

---

## Overview

GitHub PR Review Context MCP gives AI assistants institutional review memory.

Instead of generic feedback, reviews are informed by historical reviewer comments, recurring quality patterns, and repository-specific standards from your own PR history.

### Core Value

- Improves review consistency across teams and repositories.
- Reduces repeated reviewer feedback on known issues.
- Accelerates onboarding for new contributors.
- Integrates with any MCP-compatible client and multiple LLM providers.

---

## Key Capabilities

| Capability | What It Delivers |
|---|---|
| Historical review retrieval | Semantic search across prior PR comments and review summaries |
| Context-aware AI review | Feedback grounded in repository-specific review behavior |
| Smart repository readiness | Auto-detect indexed state and index on demand |
| Flexible storage modes | Permanent (disk) and temporary (in-memory) indexing options |
| Portable inference layer | Switch LLM providers using environment configuration only |

---

## Demo

![demo](assets/demo.gif)

Example workflow:
- Ask the assistant to review a diff using repository history.
- The server retrieves similar past review context.
- The model returns grounded feedback aligned to team expectations.

---

## Documentation

Detailed guides are split into focused pages:

- [Quick Start and Usage](quickstart.md)
- [LLM Configuration](llm-configuration.md)
- [Integrations](integrations.md)
- [Architecture and Tools](architecture.md)
- [Pipeline Deep Dive](pipeline.md)
- [Roadmap](roadmap.md)

---

## Quick Links

- Access setup: [GitHub Token Guide](GUIDE_GITHUB_TOKEN.md)
- Client connection: [Integrations](integrations.md)
- System internals: [Architecture and Tools](architecture.md)
- Execution flows: [Pipeline Deep Dive](pipeline.md)
- First run: [Quick Start and Usage](quickstart.md)

---

## License

MIT
