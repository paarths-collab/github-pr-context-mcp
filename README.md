# Pitfall

> Formerly *GitHub PR Context*. The package and commands keep their old names until the renamed package is published; `pitfall-mcp` works as a command alias today.

Your agent learns from your repository's past pull requests so it stops repeating mistakes your team already paid for.

1. **Learn from all past PRs.** The agent walks the repo's closed and merged PRs oldest-first in batches, reads each PR's reviews, inline review threads, change requests, reverts and follow-up fixes through the **official GitHub MCP**, and writes down lessons: concrete rules ("mistakes made here before") with evidence links.
2. **Keep learning.** Once the backfill is complete, the same loop processes only PRs newer than its cursor.
3. **Harden and alert on repeats.** If a new PR's review shows a mistake an existing lesson already covers, the lesson is hardened (repeat count, stronger wording, promoted to a "Must not repeat" section, more evidence) and you get an alert: "this type of mistake happened again (lesson L3, PR #57 link) - you may want to look at it". A new kind of mistake becomes a new lesson.
4. **Use the lessons.** Before writing code, the agent loads the repo's lessons and checks its plan against them, and tells you which ones it applied.

GitHub's official MCP provides search and reads. This package's default command is a small MCP server with no GitHub access of its own: it holds the workflow, validates and stores lessons in a local JSON file, renders `LESSONS.md`, and raises alerts. Your agent does the reading and the reasoning.

## Quick start

1. Connect the [official GitHub MCP](https://github.com/github/github-mcp-server) in your IDE using its supported authentication flow. Read-only access scoped to the repositories you need is enough.
2. Add this server (`github-pr-context-mcp`, see below) to the same client.
3. Run the prompt `learn_from_prs` with `repo` set to `owner/name`. It does one batch: status, search, read reviews and inline threads, extract lessons and repeats, record them, report alerts. Run it again until it says the repo is caught up (or tell it to keep going). Optionally schedule it (for example daily) in a client that can run recurring agent tasks, so new PRs are learned automatically.
4. Before changing code, run the prompt `check_lessons_before_change` (`repo`, `task`), or have your agent follow the [skill](.agents/skills/github-pr-context/SKILL.md): it loads the lessons first and reports any it applied.
5. To look up plain PR history without the learning loop, use the prompt `find_pr_context` or ask: "Use github-pr-context to find why owner/repo handles retries this way."

Server tools (all return JSON): `learning_status(repo)`, `record_learning(repo, processed_prs, new_lessons, repeats, backfill_complete=False)`, `get_lessons(repo)`, `list_alerts(repo)`, `acknowledge_alert(repo, alert_id)`, `remove_lesson(repo, lesson_id, reason)`. Resource: `pr-context://lessons/{owner}/{repo}`.

## Where lessons live

Per repository, under `~/.github-pr-context/lessons/<owner>__<repo>/` (override the base with `GITHUB_PR_CONTEXT_HOME`):

- `state.json`: phase (`backfill` or `ongoing`), cursor, processed PR numbers, lessons, alerts. Written atomically.
- `LESSONS.md`: the rendered lessons, "Must not repeat" first.

```sh
github-pr-context-mcp lessons --repo owner/name            # path and content of LESSONS.md
github-pr-context-mcp lessons --repo owner/name --alerts   # open alerts
```

## Limits

- The host model does the learning. Lesson quality depends on the model and on how thoroughly it reads each PR; lessons can be wrong, stale or too general. The skill asks for reviewer, revert or fix evidence, but nothing here can verify it. Check the evidence links.
- Lessons are local to your machine and are not shared with teammates or other tools.
- GitHub search can lag recent updates, returns at most 1000 results per query (the cursor moves the date filter forward), and can miss history. PRs that were still open when the cursor passed them are not picked up automatically.
- Review text is untrusted. Lesson text is stripped of control characters, length-capped and escaped when rendered, and URLs must point into the repo, but a lesson is still advisory data, never an instruction. The user's request always wins.
- Exact-match deduplication only; similar-looking rules are flagged to the agent as possible duplicates, and `remove_lesson` soft-deletes.

## Optional workflow companion

The skill works directly with the official GitHub MCP, but the learning loop needs this companion MCP for its lessons store (it also serves prompts and resources for clients that use those instead of skills):

```sh
pipx install github-pr-context-mcp
github-pr-context-mcp
```

The default command serves the prompts `find_pr_context(repo, task)`, `learn_from_prs(repo)` and `check_lessons_before_change(repo, task)`, the resource `pr-context://workflow`, and the lessons tools above over stdio. It has no GitHub API tools and does not invoke another MCP server automatically: the host agent must have the official GitHub MCP connected separately and follow the workflow.

```sh
# Print an example read-only official GitHub connection; does not edit client config.
github-pr-context-mcp config
# Copy the bundled skill into this project; refuses to overwrite an existing skill.
github-pr-context-mcp install-skill --skill-dir .agents/skills
```

Example for clients supporting remote MCP URLs and custom headers (authentication and config syntax vary by client):

```json
{
  "mcpServers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "X-MCP-Readonly": "true",
        "X-MCP-Toolsets": "repos,pull_requests"
      }
    }
  }
}
```

See the [official remote-server documentation](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md) for supported client setup. Never paste credentials into a chat or skill. No user configuration or GitHub connection is changed by installing this package.

## Legacy indexed mode

The earlier indexed implementation remains available explicitly:

```sh
pipx install 'github-pr-context-mcp[indexed]'
github-pr-context-indexed
```

Existing configurations running `github-pr-context-mcp` for indexed tools must switch their command to `github-pr-context-indexed` and install the `indexed` extra. Existing indexes are preserved. The default command now offers prompts/resources rather than the previous indexed tools.

See [legacy indexed documentation](docs/legacy-indexed.md) and the explicitly selected [legacy skill](.agents/skills/github-pr-context-v3/SKILL.md). This mode retains its own authentication, local storage, and model dependencies. Deployment and older pipeline documents describe that mode.

## Development

```sh
python -m pip install '.[test,indexed]'
python -m pytest
E2E_RUN=1 python -m pytest tests/e2e -s
```

The ordinary suite includes a real stdio session for the companion and tests for the learning loop (temporary data directory). The opt-in indexed stdio harness exercises the installed legacy command with stubbed GitHub responses. Neither requires live GitHub writes.

PR content is untrusted evidence. The workflow does not authorize GitHub writes or local command execution, and builds no PR index or embeddings; the only local state is the lessons file. Host conversation history and GitHub's own retention are separate.
