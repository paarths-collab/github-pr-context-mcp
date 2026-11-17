# How Pitfall decides what becomes a rule

This is the full reference behind the short version in the [README](../README.md).

## Who decides what

The model judges meaning, a fresh checker pass verifies it, and code enforces the facts. The server cannot call a model, so "model" and "checker" are your host agent: the checker is a fresh pass (a sub-agent if the host has one, otherwise a separate step) that sees only the pending items, never the learner's reasoning.

| | Who decides | Rule |
| --- | --- | --- |
| Learned | Model labels observations | `{pr, url, date, kind, reviewer, author, paths, rule, why, quote, example?, lesson_id?, contradicts?}`; kind is `revert`, `follow_up_fix`, `change_request_fixed` or `review_comment`; the quote is verbatim, 300 characters at most |
| Discarded | Code | `review_comment` (weak, counted in stats only), reviewer is the PR author, bot reviewer (unless revert or follow-up fix), missing quote (except for a revert), URL outside the repo. The same PR counts once per lesson |
| Needs a check | Checker (`list_pending_checks`, `submit_checks`: approve, edit or reject) | a new proposed lesson, a rule-text edit, a repeat claim on an active lesson, a contradiction claim. Nothing counts, serves or alerts until approved; a rejected claim is not re-queued for the same PR. More evidence for an approved lesson needs no re-check |
| Lifecycle | Code, from approved evidence in distinct PRs | candidate: one strong signal, never served. active: strong evidence in 2 PRs, or a revert, or `approve_lesson`. hardened: active plus an approved repeat. archived: newest evidence over 365 days older than the newest processed PR (not the wall clock, so backfills are deterministic), or a conflict resolved "archive"; new evidence revives it. removed: manual, permanent |
| Team or preference | Code | evidence from 2 or more distinct reviewers is a team rule; otherwise a reviewer preference (shown as such, score halved) |
| Score | Code | (3 x reverts + 2 x follow-up-fix PRs + 1 x change-request PRs + 0.5 x distinct reviewers + 2 x approved repeats) x 0.5^(age_days/365) x (0.5 if preference), rounded to 2 places and shown |
| Scope | Code | evidence paths become directories (3 levels deep at most); a lesson touching 3 or more top-level directories is global. Lessons with no recorded paths are treated as global |
| Conflicts | Checker, then you | an approved contradiction opens a `conflict` alert; `resolve_conflict` with `keep` or `archive` |
| Served | Code (`get_relevant_lessons(repo, paths, limit=8)`) | active and hardened lessons in scope, hardened first then by score, at most `limit` (1 to 20) and about 600 tokens; each is `{id, rule, why (160 characters), status, kind, score, link, example?}` with the best evidence link (revert, then fix, then change request) and the example only if it still fits. The omitted count is reported. Candidates, archived and unchecked lessons are never served |

Candidates are listed by `list_candidates`; promote one with `approve_lesson` or reject it with `remove_lesson`.


## Server tools, prompts and resources

Server tools (all return JSON): `learning_status(repo)`, `record_learning(repo, processed_prs, observations, backfill_complete=False)` (the older `new_lessons` and `repeats` parameters still work and become observations), `list_pending_checks(repo)`, `submit_checks(repo, decisions)`, `get_relevant_lessons(repo, paths, limit=8)`, `get_lessons(repo)` (full inspection listing), `list_candidates(repo)`, `approve_lesson(repo, lesson_id)`, `list_alerts(repo)`, `acknowledge_alert(repo, alert_id)`, `resolve_conflict(repo, alert_id, decision)`, `remove_lesson(repo, lesson_id, reason)`. Prompts: `learn_from_prs`, `check_pending_rules`, `check_lessons_before_change`, `find_pr_context`. Resource: `pr-context://lessons/{owner}/{repo}`.

## Where lessons live

Per repository, under `~/.github-pr-context/lessons/<owner>__<repo>/` (override the base with `GITHUB_PR_CONTEXT_HOME`):

- `state.json` (schema 2; schema 1 files are migrated on the next write): phase (`backfill` or `ongoing`), cursor, processed PR numbers, lessons with their approved evidence, pending checks, decided claims, alerts. Written atomically under a lock.
- `LESSONS.md`: the rendered lessons: Must not repeat, Rules, Candidates (not yet rules), Pending checks (count), Archived, plus the discarded and weak-comment counts.

```sh
github-pr-context-mcp lessons --repo owner/name            # path and content of LESSONS.md
github-pr-context-mcp lessons --repo owner/name --alerts   # open alerts
github-pr-context-mcp lessons --repo owner/name --export AGENTS.md
```

`--export PATH` (also available as `pitfall-mcp`) writes up to 30 active and hardened lessons, best score first, into a managed block between `<!-- pitfall:lessons:start -->` and `<!-- pitfall:lessons:end -->`, with a header saying they are advisory and derived from PR reviews. It creates the file if missing, replaces only that block, and is idempotent. It is a CLI command only, not an MCP tool.

