# Architecture and Tools

<div align="center">

![MCP](https://img.shields.io/badge/Protocol-MCP-green)
![GitHub GraphQL](https://img.shields.io/badge/Data-GitHub%20GraphQL-black?logo=github)
![Vector DB](https://img.shields.io/badge/Vector-ChromaDB-orange)
![Embeddings](https://img.shields.io/badge/Embeddings-all--MiniLM--L6--v2-purple)
![Inference](https://img.shields.io/badge/Inference-Multi--Provider-blue)

**Context-aware code review architecture powered by retrieval from historical PR discussions.**

</div>

---

## Table of Contents

- [System Overview](#system-overview)
- [Architecture Diagram](#architecture-diagram)
- [Indexing Pipeline](#indexing-pipeline)
- [Review Pipeline](#review-pipeline)
- [Storage Design](#storage-design)
- [MCP Tools](#mcp-tools)
- [Project Structure](#project-structure)

---

## System Overview

This project turns repository PR history into semantic memory for code review.

High-level flow:
1. Fetch PR + review artifacts from GitHub GraphQL.
2. Transform them into searchable documents.
3. Embed and store in ChromaDB.
4. Retrieve relevant historical context for a new snippet/diff.
5. Generate review output through the selected LLM provider.

---

## Architecture Diagram

This diagram shows the end-to-end system topology across client, MCP server modules, and external services.

```mermaid
flowchart TB
    subgraph Client["AI Client via MCP"]
        U[User Prompt]
        A[Assistant]
    end

    subgraph Server["MCP Server"]
        S["server.py - Tool Routing + Session State"]
        F["fetcher - GitHub GraphQL Fetch"]
        T["fetcher/transform.py - Normalize PR Data"]
        D["storage/document_builder.py - Build Documents + Metadata"]
        E["storage/encoder.py - SentenceTransformer"]
        V["storage/vector_store.py - Chroma Query + Upsert"]
        R["inference/review.py - Prompt Assembly"]
        P["inference/providers.py - Provider Adapter"]
    end

    subgraph External["External Services"]
        GH[(GitHub GraphQL API)]
        DB[(ChromaDB)]
        LLM[(LLM Providers)]
    end

    U --> A --> S
    S --> F --> GH
    F --> T --> D --> E --> V --> DB
    S --> V
    S --> R --> P --> LLM
    R --> S --> A --> U
```

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

    C->>S: ensure_repo_ready(repo, storage, pages)
    S->>S: Check existing Permanent/Temporary index
    alt Not Indexed
        S->>G: Fetch PR pages (30/page)
        G-->>S: PR nodes + review threads + reviews + files
        S->>X: flatten_prs(nodes)
        X-->>S: normalized PR dictionaries
        S->>B: build_documents(prs)
        B-->>S: docs, metadata, ids
        S->>M: encode(doc) per document
        M-->>S: embeddings
        S->>V: upsert(documents, embeddings, metadatas, ids)
    end
    S-->>C: repo activated and ready
```

### Indexed Document Types

| Type | Source | Typical Content |
|---|---|---|
| `pr_description` | PR title/body | Feature intent, rationale, scope |
| `review_comment` | Inline thread comments | File/line-specific reviewer feedback |
| `review_summary` | Review body | High-level approval/request changes reasoning |

---

## Review Pipeline

This sequence illustrates how retrieved historical context is converted into a grounded, repository-aware review response.

```mermaid
sequenceDiagram
    participant C as Client
    participant S as review_code_with_history
    participant V as query_similar
    participant P as Prompt Builder
    participant L as LLM Provider

    C->>S: review_code_with_history(code, repo?)
    S->>V: Semantic retrieval (n_results=10)
    V-->>S: similar docs + metadata + distances
    S->>P: keep top context slice (first 6)
    P-->>S: grounded review prompt
    S->>L: chat(messages, system, max_tokens)
    L-->>S: review response
    S-->>C: context-aware review
```

### Retrieval and Prompting Notes

| Step | Implementation Detail | Why It Matters |
|---|---|---|
| Similarity metric | cosine distance converted to similarity via `1 - distance` | Human-readable relevance score |
| Retrieval depth | Top 10 retrieved, top 6 used in review prompt | Balances recall vs token budget |
| Prompt style | Senior reviewer, concrete issues, low-fluff | Better actionable review quality |

---

## Storage Design

### Permanent vs Temporary Index

| Mode | Backing Client | Persists Restart | Best Use Case |
|---|---|---|---|
| Permanent | `chromadb.PersistentClient` | Yes | Repeatedly used repositories |
| Temporary | `chromadb.EphemeralClient` | No | One-off exploration |

### Collection Naming Strategy

`owner/repo` is normalized to `owner--repo` to generate safe collection names.

---

## MCP Tools

| Tool | Primary Purpose | Key Inputs | Typical Output |
|---|---|---|---|
| `ensure_repo_ready` | Auto-load or index a repository and activate session context | `repo`, optional `storage`, optional `pages` | Active repo state + indexing summary |
| `set_active_repo` | Switch context to an already indexed repo | `repo` | Active repo switched confirmation |
| `list_indexed_repos` | List all indexed repos (both storage modes) | none | Repo list + storage labels + doc counts |
| `semantic_search_reviews` | Semantic retrieval over historical review artifacts | `query`, optional `repo`, optional `n_results` | Ranked context snippets |
| `review_code_with_history` | Generate review using retrieved team history | `code`, optional `repo` | Grounded code review text |
| `get_team_review_patterns` | Summarize recurring review patterns | optional `repo`, optional `topic` | Pattern summary |
| `get_index_stats` | Show indexed document count for a repo | optional `repo` | Stats JSON |

<details>
<summary><strong>Interactive Tool Contracts (expand)</strong></summary>

### `ensure_repo_ready`
- Checks permanent first, temporary second.
- If not found and `storage` missing, prompts user to choose permanent vs temporary.
- If storage provided, fetches PRs and indexes immediately.

### `review_code_with_history`
- Retrieves similar historical comments.
- Injects context into a review-focused system prompt.
- Calls active LLM provider adapter.

### `semantic_search_reviews`
- Returns metadata-rich snippets with similarity score.
- Useful for manual inspection before invoking full review.

</details>

---

## Project Structure

```text
github-pr-context-mcp/
├── server.py              # MCP server: tool definitions + session routing
├── indexer.py             # CLI indexer for manual pre-fetch/index
├── fetcher/
│   ├── client.py          # GraphQL client, pagination, errors
│   ├── queries.py         # GraphQL query strings
│   └── transform.py       # Raw node -> normalized PR shape
├── storage/
│   ├── encoder.py         # SentenceTransformer embedding
│   ├── document_builder.py# Build text docs/metadata/ids
│   └── vector_store.py    # Chroma indexing/query/stats
└── inference/
    ├── providers.py       # Unified multi-provider chat adapter
    └── review.py          # Prompt assembly + review/pattern summarization
```

---

Back to [README](README.md)
