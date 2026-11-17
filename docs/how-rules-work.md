# How Pitfall decides what becomes a rule

This is the full reference behind the short version in the [README](../README.md).

## Who decides what

The model judges meaning, a fresh checker pass verifies it, and code enforces the facts. The server cannot call a model, so "model" and "checker" are your host agent: the checker is a fresh pass (a sub-agent if the host has one, otherwise a separate step) that sees only the pending items, never the learner's reasoning.

| | Who decides | Rule |
| --- | --- | --- |
| Learned | Model labels observations | `{pr, url, date, kind, reviewer, author, paths, rule, principle?, family?, why, quote, example?, lesson_id?, family_repeat?, merge_with?, contradicts?}`; kind is `revert`, `follow_up_fix`, `change_request_fixed` or `review_comment`; the quote is verbatim, 300 characters at most |
| Discarded | Code | `review_comment` (weak, counted in stats only), reviewer is the PR author, bot reviewer (unless revert or follow-up fix), missing quote (except for a revert), URL outside the repo. The same PR counts once per lesson |
| Needs a check | Checker (`list_pending_checks`, `submit_checks`: approve, edit or reject) | a new proposed lesson (rule, principle and family), a rule, principle or family edit, a repeat claim on an active lesson, a family repeat claim, a merge, a contradiction claim. Nothing counts, serves or alerts until approved; a rejected claim is not re-queued for the same PR. More evidence for an approved lesson needs no re-check |
| Lifecycle | Code, from approved evidence in distinct PRs | candidate: one strong signal, never served. active: strong evidence in 2 PRs, or a revert, or `approve_lesson`. hardened: active plus an approved repeat. archived: newest evidence over 365 days older than the newest processed PR (not the wall clock, so backfills are deterministic), or a conflict resolved "archive"; new evidence revives it. removed: manual, permanent |
| Team or preference | Code | evidence from 2 or more distinct reviewers is a team rule; otherwise a reviewer preference (shown as such, score halved) |
| Score | Code | (3 x reverts + 2 x follow-up-fix PRs + 1 x change-request PRs + 0.5 x distinct reviewers + 2 x approved repeats) x 0.5^(age_days/365) x (0.5 if preference), rounded to 2 places and shown |
| Scope | Code | evidence paths become directories (3 levels deep at most); a lesson touching 3 or more top-level directories is global. Lessons with no recorded paths are treated as global |
| Conflicts | Checker, then you | an approved contradiction opens a `conflict` alert; `resolve_conflict` with `keep` or `archive` |
| Served | Code (`get_relevant_lessons(repo, paths, limit=8, families=None)`) | active and hardened lessons in scope for the paths, global lessons, and lessons of the requested families from any folder; see [Serving by kind](#serving-by-kind). Candidates, archived, merged and unchecked lessons are never served |

Candidates are listed by `list_candidates`; promote one with `approve_lesson` or reject it with `remove_lesson`.

## Principle and specific rule

A lesson has two levels of wording:

- **specific** (`rule`): what happened in that PR, for example "Pass `timeout=` to `requests.get` in the API client". Lessons created before families keep their rule text here.
- **principle**: the general rule behind it, for example "Every outbound network or database call needs a timeout". If none is given the principle is the rule.

The learner supplies both (`rule`, `principle`). The checker sees both for every new-lesson check and must confirm that the principle is supported by the quotes and evidence and is not broader than them. It can fix either (and the family) with verdict `edit` and `rule`, `principle` and/or `family`; whatever it leaves out stays as proposed. An observation that names an existing lesson and adds new evidence can carry a different principle or a family other than `other`; that is queued as a `rule_edit` check, so approved lessons (including those migrated from schema 2) can be reclassified once the checker approves.

## Mistake families

Every lesson has exactly one family from a fixed list in `entrypoints/local/rules.py` (`FAMILIES`, returned by the `list_families` tool). The model labels the family, the checker verifies or changes it, and code only stores and counts it. A missing or unknown family (anything not in the list, after lower-casing and treating spaces and underscores as hyphens) becomes `other`.

| Family | Kind of mistake |
| --- | --- |
| `missing-timeout` | outbound calls (HTTP, database, RPC, subprocess) made without a timeout |
| `blocking-io` | blocking or synchronous work on a hot path, an event loop or a UI thread |
| `resource-leak` | files, connections, locks, threads or memory that are not released or closed |
| `error-handling` | errors swallowed, wrong exceptions, missing retries, fallbacks or cleanup |
| `concurrency` | races, missing locks, shared mutable state or non-atomic updates |
| `input-validation` | untrusted or malformed input that is not validated, bounded or sanitised |
| `security` | secrets, authentication, authorisation, injection or other security weaknesses |
| `api-misuse` | a library, framework or internal API used against its contract |
| `performance` | needless work such as N+1 queries, repeated calls or unbounded loops and buffers |
| `data-integrity` | data loss, corruption, inconsistent state or unsafe migrations |
| `naming-and-style` | naming, formatting, structure and readability conventions |
| `tests` | missing, weak, flaky or misplaced tests |
| `docs-and-changelog` | missing or outdated documentation, comments or changelog entries |
| `config-and-deps` | configuration, environment, build or dependency problems |
| `other` | anything that fits none of the families above |

### Family stats

Code computes, per family: lessons (active or hardened), hardened lessons, approved repeats (summed over the family's lessons) and the date of the newest repeat. Families with nothing to report are left out. The numbers are in `get_lessons` and `learning_status` (`families`) and in the "Mistake families" table of `LESSONS.md`.

## Family repeats

A repeat observation can name a `lesson_id` (the same mistake as that lesson) or only a family: `family` set, `family_repeat: true` and no `lesson_id`. (An observation with a family, no `lesson_id` and no `why` is also read as a family repeat, because a new lesson needs a why; a rule text that equals an existing lesson is a repeat of that lesson.) It claims: "this PR made the same kind of mistake as the rules in this family".

1. Code queues a `family_repeat` check, if the family is not `other` and has an active or hardened lesson; otherwise the observation is discarded. The check shows the family description and up to 5 related lessons (rule and principle).
2. The checker judges "same kind?": would the principle of a related lesson have prevented this? It approves (optionally naming `lesson_id`, which must be an active or hardened lesson of that family) or rejects.
3. On approval code hardens the named lesson, or else the strongest active or hardened lesson of the family (hardened first, then score) that the PR is not already evidence for. If there is none, the repeat is skipped.
4. After the backfill it alerts: "This kind of mistake (<family description>) happened again in PR #N (related lesson Lx) - you may want to look at it." The alert has the usual fields plus `family`. During the backfill it hardens quietly, as lesson repeats do (the phase when the claim was recorded decides).
5. Idempotent per (family, PR): a PR is one claim for a family (pending claims attach), a decided claim is never re-queued (a rejected one is discarded), and a PR counts at most once per lesson.

Repeats of a single lesson (`lesson_id`) behave as before, with the older wording "This type of mistake happened again (lesson Lx, PR #N <url>) - you may want to look at it."

## Serving by kind

`get_relevant_lessons(repo, paths, limit=8, families=None)`. `families` is a list of names from `list_families`; unknown names are ignored and reported in `unknown_families`. The agent first names the families its change involves, then asks with paths and families (the `check_lessons_before_change` prompt does this).

- **Selection.** Active and hardened lessons whose scope matches the paths (as before), global (or unscoped) lessons, and active or hardened lessons in a requested family from anywhere in the repo. Without `families`, behaviour is the folder-only behaviour of earlier versions.
- **Order.** Hardened first, then folder and global matches before family-only matches, then score (then lesson id).
- **`matched_by`.** `path` (folder match), `global`, or `family` (only the requested family brought it here: apply the principle, not the details). A lesson that matches both by folder and family is `path`.
- **Limit and budget.** At most `limit` (1 to 20) and about 600 tokens (chars / 4). Each entry is `{id, principle, family, why (160 characters), status, kind, score, link, matched_by}`; the list ends at the first entry that does not fit. After that, the specific `rule` (omitted when identical to the principle) is added to the best-ranked entries while it fits, then the example. So the principle is preferred when the budget is tight. The omitted count is reported.

## Merging similar lessons

The same mistake can be learned twice in different words. A merge is proposed by the model, verified by the checker and applied by code:

- **Proposal.** An observation with `merge_with: [ids]` and `principle` (merges `lesson_id` and the listed lessons), or `propose_merge(repo, lesson_ids, principle, reason)`. Both queue a `merge` check; nothing changes yet. Two to ten distinct, existing, unremoved, unmerged lessons are needed. A pending merge of the same lessons is not duplicated, and a rejected one is not proposed again.
- **Code refuses cross-family merges**, when proposing and again when approving (an unapproved merge of lessons whose families diverged stays pending until the checker rejects it). To merge across families, the checker first has to put the lessons into one family with an `edit`.
- **Check.** The checker sees each lesson's rule, principle and up to five pieces of evidence. It approves, edits the principle (`edit` with `principle`) or rejects.
- **Effect.** The strongest lesson (hardened first, then score) keeps its id. Evidence and repeats of the others move into it (a PR still counts once, the evidence cap of 30 and repeat cap of 200 apply), `manual_active` carries over, the approved principle replaces its principle and its specific rule stays. The others get `merged_into` and `merged_from` records the sources. Merged lessons are never served, are left out of listings (`get_lessons` lists them under `merged`) and `LESSONS.md`, and cannot be approved, removed or merged again. Pending checks, observations that name them, and new lessons with their rule text now count for the surviving lesson.


## Parallel backfill

A large backlog of old PRs can be learned by several helpers at once. Delegation is two-level: a **manager** (your host agent) plans and verifies, **helpers** (sub-agents, or the manager itself one chunk at a time if the host has none) read and label, and the usual checker pass and code rules decide what becomes a rule. Helpers never decide rules or approve checks.

1. The manager lists old closed PR numbers with the official GitHub search and calls `plan_backfill(repo, pr_numbers, helpers=4, chunk_size=None, replace=False)`. Code drops processed PRs, sorts the rest oldest first and splits them into chunks: `helpers` is 1 to 16, the chunk size defaults to `ceil(n / helpers)` and is at most 50, at most 2000 PRs per plan. The plan `{plan_id, created_at, chunks: [{chunk_id, prs, status, claimed_at, done_at}]}` is stored in `state.json`. Replacing a plan that still has unfinished chunks needs `replace=True`.
2. Each helper calls `claim_chunk(repo, chunk_id=None)` for a specific chunk or the next open one. The claim is atomic under the same file lock as every other write, so two helpers never get the same chunk. A claimed chunk older than 2 hours counts as stale and may be claimed again.
3. The helper reads each PR and calls `record_learning(..., chunk_id=...)`. Observations go through the same discard, pending-check and lifecycle logic as always. The chunk becomes `done` once all its PRs are in the processed set; otherwise it stays `claimed` and the reply lists `missing_prs`.
4. `backfill_progress(repo)` summarises the plan: chunk counts by state, stale claims, PRs left, pending checks and `ready_for_checker` (every chunk done). The manager then runs the fresh checker pass and finally `record_learning(backfill_complete=True)`, which switches the phase to ongoing. While chunks are unfinished it is refused (`backfill_complete_refused`) and the phase stays backfill.

| Chunk state | Meaning |
| --- | --- |
| `open` | not claimed yet |
| `claimed` | a helper holds it (stale after 2 hours: another helper may take over), or it is only partly recorded |
| `done` | all its PRs are processed |

Chunks may finish in any order. The cursor is the newest processed PR by creation date and the processed set prevents double counting, so out-of-order chunks give the same cursor and counts as a sequential run. Prompts: `learn_history_parallel` (manager) and `learn_history_chunk` (helper).

## Server tools, prompts and resources

Server tools (all return JSON): `learning_status(repo)`, `record_learning(repo, processed_prs, observations, backfill_complete=False, chunk_id=None)` (the older `new_lessons` and `repeats` parameters still work and become observations), `list_pending_checks(repo)`, `submit_checks(repo, decisions)`, `get_relevant_lessons(repo, paths, limit=8, families=None)`, `list_families()`, `propose_merge(repo, lesson_ids, principle, reason)`, `plan_backfill(repo, pr_numbers, helpers=4, chunk_size=None, replace=False)`, `claim_chunk(repo, chunk_id=None)`, `backfill_progress(repo)`, `get_lessons(repo)` (full inspection listing with family stats), `list_candidates(repo)`, `approve_lesson(repo, lesson_id)`, `list_alerts(repo)`, `acknowledge_alert(repo, alert_id)`, `resolve_conflict(repo, alert_id, decision)`, `remove_lesson(repo, lesson_id, reason)`. Prompts: `learn_from_prs`, `learn_history_parallel`, `learn_history_chunk`, `check_pending_rules`, `check_lessons_before_change`, `find_pr_context`. Resource: `pr-context://lessons/{owner}/{repo}`.

## Where lessons live

Per repository, under `~/.github-pr-context/lessons/<owner>__<repo>/` (override the base with `GITHUB_PR_CONTEXT_HOME`):

- `state.json` (schema 3; schema 1 and 2 files are migrated on read and rewritten on the next write; schema 2 lessons get family `other`, their rule as principle and no merge links): phase (`backfill` or `ongoing`), cursor, processed PR numbers, lessons with their approved evidence, pending checks, decided claims, alerts. Written atomically under a lock.
- `LESSONS.md`: the rendered lessons: Mistake families (table), Must not repeat, Rules, Candidates (not yet rules), Pending checks (count), Archived, plus the discarded and weak-comment counts.

```sh
github-pr-context-mcp lessons --repo owner/name            # path and content of LESSONS.md
github-pr-context-mcp lessons --repo owner/name --alerts   # open alerts
github-pr-context-mcp lessons --repo owner/name --export AGENTS.md
```

`--export PATH` (also available as `pitfall-mcp`) writes the principles of up to 30 active and hardened lessons (the best by score), grouped by family in taxonomy order and best score first within a family, into a managed block between `<!-- pitfall:lessons:start -->` and `<!-- pitfall:lessons:end -->`, with a header saying they are advisory and derived from PR reviews. It creates the file if missing, replaces only that block, and is idempotent. It is a CLI command only, not an MCP tool.

