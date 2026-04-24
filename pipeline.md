# Pipeline Deep Dive

<div align="center">

![Pipelines](https://img.shields.io/badge/Diagrams-Deep%20Dive-blue)
![Mermaid](https://img.shields.io/badge/Format-Mermaid-green)

**End-to-end operational flows for indexing, retrieval, and grounded review generation.**

</div>

---

## Table of Contents

- [Query Flow](#query-flow)
- [Indexing Flow](#indexing-flow)
- [Retrieval and Generation Flow](#retrieval-and-generation-flow)

---

## Query Flow

This flow shows request-time retrieval and response generation for grounded code review.

```mermaid
sequenceDiagram
    participant U as User
    participant C as MCP Client
    participant S as MCP Server
    participant V as Vector Store
    participant L as LLM Provider

    U->>C: Review this diff using team history
    C->>S: review_code_with_history(code, repo?)
    S->>V: query_similar(code, n_results=10)
    V-->>S: matched historical documents
    S->>S: assemble grounded prompt (top 6)
    S->>L: chat(messages, system, max_tokens)
    L-->>S: context-aware review
    S-->>C: review response
    C-->>U: final grounded review
```

---

## Indexing Flow

This flow shows repository indexing when context is missing from local storage.

```mermaid
sequenceDiagram
    participant U as User
    participant C as MCP Client
    participant S as ensure_repo_ready
    participant G as GitHub GraphQL
    participant T as Transformer
    participant B as Document Builder
    participant E as Encoder
    participant D as ChromaDB

    U->>C: Load repository context
    C->>S: ensure_repo_ready(repo, storage, pages)
    S->>S: check permanent/temporary index state
    alt Already Indexed
        S-->>C: activate existing repo context
    else Not Indexed
        S->>G: fetch PR pages (last 30 per page)
        G-->>S: PR nodes + review threads + reviews
        S->>T: flatten_prs(nodes)
        T-->>S: normalized PR objects
        S->>B: build_documents(prs)
        B-->>S: docs + metadata + ids
        S->>E: encode each doc
        E-->>S: embeddings
        S->>D: upsert documents and vectors
        D-->>S: index complete
        S-->>C: repo ready
    end
```

---

## Retrieval and Generation Flow

This flow summarizes the core RAG loop used during review generation.

```mermaid
flowchart TD
    A[Incoming code or diff] --> B[Embed query text]
    B --> C[Vector similarity search]
    C --> D[Top N retrieved artifacts]
    D --> E[Context window selection]
    E --> F[Prompt assembly with review system rules]
    F --> G[Provider adapter call]
    G --> H[LLM review output]
    H --> I[Return response to client]
```

### Operational Notes

| Stage | Current Behavior | Practical Impact |
|---|---|---|
| Retrieval depth | `n_results=10` from vector search | Strong recall with manageable token budget |
| Prompt context | Top 6 matches used in review prompt | Keeps output focused and grounded |
| Similarity score | Derived from cosine distance (`1 - distance`) | Easier ranking interpretation |
| Storage mode | Permanent or ephemeral | Flexible trade-off between speed and persistence |

---

Back to [README](README.md)
