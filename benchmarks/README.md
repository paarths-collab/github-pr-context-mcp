# PR Replay Benchmark

Measures whether retrieving a repository's own pull-request history helps an
agent resolve a new issue in that repository.

Run with [`scripts/pr_replay_benchmark.py`](../scripts/pr_replay_benchmark.py).

## Why replay merged PRs

The older harness, [`scripts/eval_harness.py`](../scripts/eval_harness.py), scores
against hand-written "known patterns" — the tasks and the rubric are both
authored by the same person trying to show the tool works. There is no ground
truth, so the result is only as good as the rubric.

This benchmark replays pull requests that a human team already merged. The real
merged diff *is* the ground truth. Nobody has to decide what a good answer looks
like; it already exists and is public.

## The experiment

Ten merged `langchain-ai/langchain` PRs are held out. For each, two agents get
an identical task — the PR title, its body, and the list of files it touched —
and must produce the change:

| Arm | Gets |
|---|---|
| `baseline` | The task only |
| `mcp` | The task plus retrieved history from this repo's earlier PRs |

A blinded judge then scores both against the real merged diff on
`functional_match` (0-10), `convention_match` (0-10) and `revision_rounds`
(0-3), in **both presentation orders**, and the two orders are averaged so the
judge's positional preference cancels out.

Both arms are told which files to change. That is deliberate: it removes
"knowing where things live in an unfamiliar monorepo" as a variable, so what
remains is whether the history improved the *content* of the change. It makes
the benchmark harder to win, not easier.

## Leakage is the whole ballgame

If the index contains the answer, the benchmark measures nothing. Three guards:

1. **Held-out PRs are never indexed.** Not filtered at query time — absent from
   the store, verified after every index build.
2. **Retrieval is hard-filtered to `pr_number < target`,** and any result that
   violates it is counted and reported rather than silently dropped.
3. **Duplicate attempts are excluded.** langchain contributors routinely re-file:
   a PR is opened, closed, and the same change lands under a new number. Four of
   the ten targets had exactly this — a *closed* twin in the corpus carrying the
   same `Fixes #NNNN` reference, sometimes the identical commit message. Seven
   such PRs are detected and removed from the index. Leaving them in would not
   test retrieval, it would test copying.

Guard 2 is a proxy for "merged before the target", not a proof. PR numbers are
assigned at open time, so a lower-numbered PR could in principle merge after the
target. Targets are drawn from the newest merged PRs to keep that window small.
The benchmark does not claim a strict temporal cut.

## Task selection

`--select` re-derives the task list from the corpus. Automated PRs
(`chore(model-profiles)`, `release(...)`, dependency bumps) are skipped: they
encode no engineering decision to replay. What remains must touch Python, change
8-400 lines across 1-6 files, and carry a body that states the problem.

The resulting ten PR numbers are pinned in the script so results stay
reproducible as langchain moves.

## Arms

    --arm tool     the model calls search_pr_history() itself (default; this is
                   how the MCP server is actually used)
    --arm inline   the same retrieval injected into a single-turn prompt

Running both separates "the history did not help" from "this model degrades over
a multi-turn tool exchange". Those are very different conclusions and the tool
arm alone cannot tell them apart.

A third path drives Claude in the IDE as the coding agent, via isolated
subagents rather than an API loop:

    --prepare-claude     lay out per-arm task/history/ground-truth files
    --aggregate-claude   read the subagent verdicts and do the arithmetic

Each solver subagent gets its own directory containing exactly what its arm is
entitled to see. The baseline solver has no path to the history file, and no
solver has a path to the ground truth. The script does only the deterministic
parts — no arm computes its own score.

## Usage

```bash
python scripts/pr_replay_benchmark.py --fetch    # cache the PR corpus
python scripts/pr_replay_benchmark.py --index    # embed corpus minus held-out
python scripts/pr_replay_benchmark.py --run      # run the A/B
```

`--fetch` needs `GITHUB_TOKEN`; `--run` needs `GROQ_API_KEY`.

## Reading the output

`replay_summary_<arm>_<run_id>.json` carries the headline numbers and, next to
them, the things that would invalidate those numbers:

- `judge_noise.mean_order_gap` — how much the judge disagreed with itself
  between the two presentation orders. **Treat any delta smaller than this as
  noise.**
- `empty_answers` — a non-empty count means that arm's mean is meaningless. An
  empty answer scores 0 everywhere, which looks like catastrophic failure and is
  actually a harness bug.
- `leakage.retrieval_violations` — must be 0.
- `record` — win/loss/tie on functional match, which is more robust to one
  outlier task than the mean is.

A benchmark whose author picks the tasks and the rubric can usually be made to
say whatever the author wants. These fields exist so a reader can check this one
rather than trust it.

## Result — Claude arm, 2026-07-29

Coding agent and judge were both Claude, run as isolated subagents.
Raw data: `replay_results_claude_20260729_045659.csv`, and the full evidence
trail (every task, patch, ground-truth diff and verdict) under `claude_run/`.

| Metric | Baseline | With PR history | Delta |
|---|---:|---:|---:|
| Functional match | 6.60 / 10 | 6.90 / 10 | **+0.30** |
| Convention match | 6.00 / 10 | 6.35 / 10 | **+0.35** |
| Revision rounds | 1.70 | 1.50 | **0.20 fewer** |

Win/loss on functional match: **6 wins, 2 losses, 2 ties.**
Retrieval leak violations: 0. Empty answers: 0.

### This does not show a significant improvement

Every metric leans the same way, and retrieval won more tasks than it lost. But
the effect is too small to distinguish from noise at this sample size, and the
honest read is *no demonstrated effect*, not *a small positive effect*:

- **The judge disagreed with itself by 1.00 on average** between the two
  presentation orders — larger than the +0.30 functional gap being claimed. A
  difference smaller than the measuring instrument's own scatter is not a
  measurement.
- **95% CI on functional delta is [-0.32, +0.92]** — it crosses zero.
- **The 6-2 record is not significant.** A sign test over the 8 decisive tasks
  gives p = 0.145 against a coin flip. Ten tasks cannot resolve an effect this
  small; it would take roughly 60-100 to detect a delta of this size.

### What the per-task numbers suggest

Convention match moved on 7 of 10 tasks and was negative on only one, while
functional match was flat or negative on 4. If there is a real effect here, the
shape of it is that history teaches *house style* — which helper to reach for,
how this team writes its tests — and not how to diagnose a bug. That is a
plausible mechanism rather than a demonstrated one.

The single worst task for retrieval, #39101 at -2.0, is also the task where the
judge contradicted itself most (order gap 4.0, the highest in the run), so it is
simultaneously the strongest evidence against and the least trustworthy data
point. Dropping it would lift the headline, which is exactly why it is kept.

### Also worth knowing

An earlier run of this same benchmark against `llama-3.3-70b-versatile` had the
retrieval arm losing badly, including two scores of 0.0. Those zeros were a
harness bug — the arm exhausted its tool-call budget and the loop returned an
empty string, which the judge dutifully scored as a total failure. The
`empty_answers` field exists so that failure mode can never again be mistaken
for a result.
