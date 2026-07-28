# Architecture and Tools

<div align="center">

![MCP](https://img.shields.io/badge/Protocol-MCP-green)
![GitHub GraphQL](https://img.shields.io/badge/Data-GitHub%20GraphQL-black?logo=github)
![Vector DB](https://img.shields.io/badge/Vector-ChromaDB-orange)
![Embeddings](https://img.shields.io/badge/Embeddings-all--MiniLM--L6--v2-purple)
![Architecture](https://img.shields.io/badge/Architecture-Pure--Context-blue)

**Pure-context architecture providing historical PR discussion retrieval to IDE agents.**

</div>

---

## Table of Contents

- [System Overview](#system-overview)
- [Architecture Diagram](#architecture-diagram)
- [Indexing Pipeline](#indexing-pipeline)
- [Retrieval Pipeline](#retrieval-pipeline)
- [Storage Design](#storage-design)
- [MCP Tools](#mcp-tools)
- [Project Structure](#project-structure)

---

## System Overview

This project turns repository PR history into semantic memory for IDE agents.

Unlike traditional RAG systems that perform inference on the server, this architecture follows a **Pure Context** model:
1. Fetch PR + review artifacts from GitHub GraphQL.
2. Transform them into searchable documents and embed them locally.
3. Store in ChromaDB.
4. Retrieve relevant historical context as **raw JSON material**.
5. The **IDE Agent** (Cursor, Claude, etc.) performs the final reasoning, review, or code generation.

---

## Architecture Diagram

This diagram shows the flow from retrieval to the IDE agent's decision-making.

```mermaid
flowchart TB
    subgraph Client["IDE Agent via MCP"]
        U[User Prompt]
        A[Agent Reasoning]
    end

    subgraph Server["MCP Server"]
        S["app/mcp_app.py"]
        AU["auth/github_device_flow.py"]
        AN["analytics/usage_metrics.py"]
        F["fetcher/client.py"]
        T["fetcher/transform.py"]
        D["storage/document_builder.py"]
        E["storage/encoder.py"]
        V["storage/vector_store.py"]
    end

    subgraph External["External Services"]
        GH[(GitHub GraphQL API)]
        KV[(OS credential vault)]
        DB[(ChromaDB)]
    end

    U --> A --> S
    S --> AU --> KV
    AU --> GH
    S --> AN
    S --> F --> GH
    F --> T --> D --> E --> V --> DB
    S --> V
    V --> S --> A --> U
```

### Component Legend

| Node | Responsibility |
|---|---|
| `app/mcp_app.py` | MCP tool routing, session state, background task orchestration |
| `auth/github_device_flow.py` | Local GitHub App Device Flow, OS-vault storage, and safe public-client token refresh |
| `auth/product_github_app.py` | Public App Client ID/slug bundled by the release maintainer; never a secret |
| `analytics/usage_metrics.py` | Multi-source anonymous usage tracking (SQLite) |
| `fetcher/client.py` | GitHub GraphQL requests, pagination, and error handling |
| `fetcher/transform.py` | Raw PR node flattening and normalization |
| `storage/document_builder.py` | Converts PR data into searchable document records |
| `storage/encoder.py` | Local embedding generation (all-MiniLM-L6-v2) |
| `storage/vector_store.py` | Chroma upsert/query and index stats |

---

## Indexing Pipeline

This sequence captures the repository onboarding path when context must be fetched, transformed, embedded, and stored.

```mermaid
sequenceDiagram
    participant C as Client
    participant S as ensure_repo_ready
    participant G as GitHub GraphQL
    participant X as Transformer
    participant B as Document Builder
    participant M as Encoder
    participant V as Vector Store

    C->>S: ensure_repo_ready(repo, storage, pages, refresh?)
    S->>S: Check existing Permanent/Temporary index
    alt Not Indexed
    S-->>C: Start background indexing task and return immediately
        S->>G: Background task: fetch newest-first PR pages (30/page)
        G-->>S: PR nodes + review threads + reviews + files
        S->>X: flatten_prs(nodes)
        X-->>S: normalized PR dictionaries
        S->>B: build_documents(prs)
        B-->>S: docs, metadata, ids
        S->>M: encode_batch(docs) locally
        M-->>S: embeddings
        S->>V: upsert() into isolated collection
    end
```

---

## Retrieval Pipeline

In the Pure Context model, the server provides the "evidence" and the IDE agent provides the "verdict."

```mermaid
sequenceDiagram
    participant C as IDE Agent
    participant S as review_code_with_history
    participant V as query_similar

    C->>S: review_code_with_history(code, repo?)
    S->>V: Semantic retrieval (n_results=12)
    V-->>S: similar docs + metadata + distances
    S-->>C: JSON Context Material
    Note over C: Agent processes raw material <br/> and generates review.
```

---

## Storage Design

### Permanent vs Temporary Index

| Mode | Backing Client | Persists Restart | Best Use Case |
|---|---|---|---|
| Permanent | `chromadb.PersistentClient` | Yes | Repeatedly used repositories |
| Temporary | `chromadb.EphemeralClient` | No | One-off exploration |

### Namespace metadata

Each repository-and-namespace pair has its own Chroma collection, so indexing, retrieval, and deletion do not share document IDs across namespaces. Hosted authorization still needs end-to-end release validation; treat the hosted multi-user path as experimental until registration and authorization coverage are complete.

---

## MCP Tools

| Tool | Primary Purpose | typical Output |
|---|---|---|
| `ensure_repo_ready` | Auto-load or background-index a repository | Active repo state + indexing status |
| `get_github_connection_status` | Check local GitHub App connection state | Token-free connection metadata |
| `begin_github_authorization` | Start local GitHub Device Flow | GitHub URL + one-time user code |
| `complete_github_authorization` | Save a browser-approved connection in the OS vault | Token-free connection metadata |
| `semantic_search_reviews` | General semantic search over historical artifacts | Raw context snippets |
| `review_code_with_history` | Retrieve context specifically for code review | JSON material + agent instruction |
| `get_team_review_patterns` | Retrieve raw material for pattern summarization | JSON patterns + agent instruction |
| `generate_code_from_history` | Retrieve context for grounded code generation | JSON context + agent instruction |
| `generate_tests` | Retrieve context for test generation | JSON patterns + agent instruction |
| `get_repo_rules_material` | Retrieve data to write .cursorrules / CLAUDE.md | High-density JSON material |
| `security_check` | Retrieve historical security discussions | JSON context + agent instruction |
| `suggest_refactors` | Retrieve past refactoring feedback | JSON context + agent instruction |

---

## Project Structure

```text
github-pr-context-mcp/
├── docs/
│   ├── architecture.md    # System design (Pure Context)
│   ├── quickstart.md      # Usage and storage guide
│   └── roadmap.md         
├── auth/
│   ├── github_device_flow.py # Local GitHub App Device Flow + OS-vault boundary
│   ├── product_github_app.py # Bundled public App identifiers only
│   └── gmail_identity.py  # Experimental hosted identity store (not GitHub credentials)
├── analytics/
│   └── usage_metrics.py   # Anonymous usage tracking
├── app/
│   ├── mcp_app.py         # MCP tool routing
│   └── tools/
│       ├── indexing.py    # Lifecycle tools
│       ├── github_auth.py # Local GitHub connection tools
│       ├── analysis.py    # Context retrieval tools
│       └── generation.py  # Context retrieval tools
├── fetcher/
│   ├── client.py          # GitHub GraphQL logic
│   └── transform.py       # PR data normalization
└── storage/
    ├── vector_store.py    # ChromaDB storage and namespace metadata
    └── encoder.py         # Local embedding generation
```

---

Back to [README](../README.md)
