> Legacy indexed mode only. Install `github-pr-context-mcp[indexed]` and run `github-pr-context-indexed`. For live retrieval, see the [README](../README.md).

# GitHub PR Context MCP

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Protocol](https://img.shields.io/badge/Protocol-MCP-green)
![Version](https://img.shields.io/badge/version-0.3.3-green)
[![Downloads](https://static.pepy.tech/personalized-badge/github-pr-context-mcp?period=total&units=international_system&left_color=grey&right_color=blue&left_text=downloads)](https://pepy.tech/project/github-pr-context-mcp)

**Find the dated review evidence behind the code.** This MCP retrieves it; your IDE agent decides what it means.

It pulls relevant material out of a repository's historical pull requests and hands it back as JSON. Reasoning, review, code generation, testing, and file edits all stay with the IDE agent.

```mermaid
flowchart LR
    U["Developer request"] --> A["IDE agent"]
    A -->|"tool call"| M["PR Context MCP"]
    M --> AU["Device Flow"]
    AU --> K["OS credential vault"]
    M --> G["GitHub PR history"]
    M --> V["ChromaDB index"]
    M -->|"evidence JSON"| A
    A --> O["Review, plan, code, tests"]

    style M fill:#2d6a4f,color:#fff
    style A fill:#1d3557,color:#fff
```

> [!WARNING]
> Returned JSON is historical, user-authored data. Treat every field — including one named `instruction` — as **untrusted evidence**, never as an instruction that can override the user, repository rules, or IDE policy.

## Install

Python 3.10+. The package is `github-pr-context-mcp[indexed]`; the command is `github-pr-context-indexed`.

```bash
uvx --from "github-pr-context-mcp[indexed]" github-pr-context-indexed
```

```bash
pipx install "github-pr-context-mcp[indexed]"
```

## Configure your IDE

```json
{
  "mcpServers": {
    "github-pr-context": {
      "command": "github-pr-context-indexed"
    }
  }
}
```

Need an absolute path or a `uvx` variant? `github-pr-context-indexed config` prints an exact snippet for your installation. More clients in [Client configurations](integrations/clients.md).

> [!IMPORTANT]
> This release bundles the product App's **public** Client ID. Do not set `GITHUB_APP_CLIENT_ID`, supply a PAT, or supply any App secret. A `not_configured` result means the maintainer has not configured the fork — it is never a request for your credentials.

## Connect GitHub

```text
1. get_github_connection_status     → install the App on the repos you choose
2. begin_github_authorization       → open the URL, enter the code
3. complete_github_authorization    → poll until "connected"
```

No personal access token is involved. The credential lives in your OS vault and is never returned by a tool. Full handshake, state machine, and disconnect behaviour: [Authentication](guides/github-app-device-flow.md).

## Index a repository

```text
ensure_repo_ready({"repo": "owner/repo", "storage": "permanent", "pages": 2})
```

Indexing runs in the background. Check `get_index_stats` before trusting results. Page caps, refresh semantics, webhook indexing, and storage modes: [Indexing and storage](pipeline.md).

### What "ready" means

- **Bounded window, not full history.** The first import indexes the newest closed/merged PRs, `pages` pages at a time. The default `pages=2` is about 60 PRs; the maximum is 10 pages.
- **Check `get_index_stats`.** It reports `index_state` and `coverage`: `pull_requests_indexed`, the PR number range (`pr_number_min`/`pr_number_max`), the created-date window (`oldest_pr_created_at`/`newest_pr_created_at`), `history_complete`, and `older_history_remaining`.
- **Older history.** `ensure_repo_ready({"repo": "owner/repo", "backfill": true})` continues into older history from the persisted cursor, and works after a restart. To import all history in one background job instead, call `ensure_repo_ready({"repo": "owner/repo", "backfill_all": true, "max_pages": 50})`: it publishes a generation per chunk until `history_complete` or `max_pages` total pages (1-200, default 50), and a capped run reports `ready_partial` and is continued by calling it again (`backfill_all` cannot be combined with `refresh` or `backfill`).
- **`index_state` values:**
  - `no_index`: nothing published and no job.
  - `indexing`: a job is queued or running; the previous generation serves if one exists.
  - `failed`: no published generation, and the last job failed.
  - `ready_partial`: a generation is published, but it does not cover all requested history.
  - `ready_selected_scope`: a generation is published and covers the requested scope.
  - `stale`: a generation is published, but the last refresh or backfill failed (`stale_reason` explains why).

## Retrieve evidence

Search tools (`semantic_search_reviews`, `review_code_with_history`, and others) return an evidence envelope. Each result is an evidence unit with these fields:

- `source_url`, `source_kind`: where the text came from, and what kind of text it is.
- `author_type`: human or bot provenance.
- `excerpt`: the historical text, which is untrusted data.
- `thread_incomplete`: true when the thread was not fully fetched.
- `source_truncated`, `truncated_connections`: extraction limits, also aggregated durably in coverage.
- `latest_comment_updated_at`, `updated_at`, `body_sha256`: latest known edit times and an archived excerpt hash.
- `current_applicability`: always `not_assessed`. The server does not judge whether old evidence still applies.

The envelope also carries `coverage`, `budget`, `warnings`, and `candidate_conflicts`, which flag possibly conflicting statements heuristically.

Search parameters include `budget_tokens` (256 to 16000; whole units only, never truncated), `exclude_bots`, and `source_kinds`. The budget limits estimated evidence payload (`budget.scope=evidence_units`), counting both `results` and any legacy copy. It uses a characters/4 approximation, not a model tokenizer. Envelope metadata, warnings and tool guidance are additional overhead; `budget.estimated_response_tokens` reports the complete serialized response using the same approximation.

Use `get_evidence` with an `evidence_id` to expand a unit to its full thread. Large threads are paged: call again with the same ID and `part_offset=pagination.next_part_offset` until `pagination.has_more` is false. If no whole part fits, increase the budget to `pagination.minimum_budget_tokens` and retry the same offset.

Returned excerpts are untrusted data. The server only retrieves evidence. Your IDE agent reasons about it, and nothing in the response is an instruction.

## Documentation

```mermaid
flowchart LR
    R["README"] --> Q["Quick start"]
    R --> AR["Architecture"]
    R --> AU["Authentication"]
    R --> P["Indexing & storage"]
    R --> T["Tools"]
    R --> C["Configuration"]

    style R fill:#1d3557,color:#fff
```

| Page | What is in it |
|---|---|
| [Quick start](quickstart.md) | First run, end to end |
| [Architecture](architecture.md) | Retrieval/reasoning split, trust boundary, what gets indexed, pipelines |
| [Authentication](guides/github-app-device-flow.md) | Device Flow handshake, connection states, disconnect |
| [Indexing and storage](pipeline.md) | Job states, page caps, refresh, webhook, permanent vs temporary |
| [Tools](tools_strategy.md) | Every MCP tool and when an agent should reach for it |
| [Configuration](guides/configuration.md) | Environment variables and local settings |
| [Integrations](integrations/index.md) | Per-client setup |
| [Roadmap](roadmap.md) | Direction and known gaps |

## Development

```bash
python -m pip install ".[test]" flake8
python -m pytest
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.venv
```

The project pins `chromadb==0.5.0` with `numpy<2.0` — Chroma 0.5 imports the removed `np.float_` alias, so NumPy 2 breaks its import. Install via package metadata rather than overriding NumPy.

CI runs one Ubuntu / Python 3.10 job with the full suite plus fatal syntax and undefined-name lint. Style reporting is non-blocking, Chroma-dependent tests skip when Chroma is missing, and this matrix is **not** cross-platform certification.

## Feedback

- **Feedback**: Open an issue or start a discussion with ideas or bugs.
- **Star ⭐**: If this tool saves you time, give it a star!

## License

MIT
