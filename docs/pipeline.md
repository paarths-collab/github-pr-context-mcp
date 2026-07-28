# v0.3 Pure-Context Pipeline

The server retrieves historical evidence. The connected IDE agent turns that evidence into a review, code change, test plan, or instruction file.

## Indexing

```mermaid
sequenceDiagram
    participant A as IDE agent
    participant AU as Local GitHub authorization
    participant S as ensure_repo_ready
    participant G as GitHub GraphQL
    participant T as Transform and document builder
    participant E as Local embedding model
    participant C as ChromaDB

    A->>AU: get_github_connection_status
    AU-->>A: connected or Device Flow required
    A->>S: ensure_repo_ready(repo, storage, pages, refresh?)
    S-->>A: Existing-index state or indexing acknowledgement
    S->>G: Fetch newest-first PR history in background
    G-->>S: PRs, reviews, comments, files
    S->>T: Normalize and build documents
    T->>E: Encode documents
    E->>C: Store embeddings and metadata
    A->>S: get_index_stats()
    S-->>A: Document count, job status, freshness, and truncation data
```

`ensure_repo_ready` starts background indexing when needed. Call it with `refresh=true` to synchronise an existing index using the GitHub `updatedAt` watermark. Use `get_index_stats` to verify that documents are available before depending on a newly requested index; its `index_job` exposes queued/running/ready/failed state and any `truncated_connections`.

## Retrieval and reasoning

```mermaid
sequenceDiagram
    participant U as Developer
    participant A as IDE agent
    participant S as MCP retrieval tool
    participant C as ChromaDB

    U->>A: Review or implementation request
    A->>S: review_code_with_history / relevant tool
    S->>C: Semantic search
    C-->>S: Historical documents and metadata
    S-->>A: JSON context material
    A->>A: Validate evidence against current code
    A-->>U: Review, code, tests, or instructions
```

The retrieval response may include an instruction field describing a useful task, but it is still historical context. The IDE agent decides whether it applies and produces the final result.

## Operational limits

- Indexing is bounded by the `pages` argument and the GitHub API response shape. When a top-level or nested connection is incomplete, v3 records `truncated_connections` alongside the evidence instead of silently treating it as complete.
- A historical pattern can become stale; compare it with current code and project instructions.
- The local embedding model supports semantic search. It does not replace the IDE agent's reasoning model.

Back to [README](../README.md)
