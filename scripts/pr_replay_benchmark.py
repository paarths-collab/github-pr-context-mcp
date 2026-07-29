"""
scripts/pr_replay_benchmark.py

Replays already-merged pull requests to measure what PR-history retrieval is
worth. Unlike scripts/eval_harness.py — whose tasks and grading rubric are both
hand-written — every task here has a ground truth that a human already merged:
the real diff.

The experiment
--------------
For each held-out merged PR, two agents receive the identical task (the PR
title, its body, and the list of files it touched) and must produce the change:

    baseline  no retrieval at all
    mcp       gets this repository's PR history, in one of two shapes:
                --arm tool    calls search_pr_history() itself (default; this is
                              how the MCP server is actually used)
                --arm inline  the same retrieval is injected into a single-turn
                              prompt, holding conversation shape equal to the
                              baseline

Both arms are told which files to touch, so the benchmark measures the content
of the change rather than the ability to navigate an unfamiliar monorepo.

A blinded judge then scores both against the real merged diff, in both
presentation orders, and the orders are averaged to cancel position bias.

Running both arms is what separates "the retrieved history did not help" from
"this model degrades over a multi-turn tool exchange" — they are very different
conclusions and the tool arm alone cannot tell them apart.

Leakage is the whole ballgame
-----------------------------
If the index contains the target PR, the MCP arm retrieves the answer and the
benchmark measures nothing. Two independent guards:

  1. The held-out PRs are never indexed at all (see build_index).
  2. Every retrieval is hard-filtered to ``pr_number < target``, and any result
     violating that is counted as a leak and reported in the summary.

Guard 2 is a *proxy* for "merged before the target". PR numbers are assigned at
open time, so a lower-numbered PR could in principle have merged after the
target. Targets are drawn from the newest merged PRs to keep that window small,
but the benchmark does not claim a strict temporal cut.

Usage
-----
    export GITHUB_TOKEN=...        # corpus fetch + ground-truth diffs
    export GROQ_API_KEY=...        # generation + judging

    python scripts/pr_replay_benchmark.py --fetch     # cache the PR corpus
    python scripts/pr_replay_benchmark.py --index     # embed corpus minus held-out
    python scripts/pr_replay_benchmark.py --run                 # tool arm
    python scripts/pr_replay_benchmark.py --run --arm inline    # inline arm

Output:
    benchmarks/replay_results_<arm>_<run_id>.csv
    benchmarks/replay_summary_<arm>_<run_id>.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import difflib
import json
import os
import re
import sys
import uuid
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = "langchain-ai/langchain"

# Isolated from any real index the user has built, so running this benchmark
# never pollutes or is polluted by day-to-day use of the server.
NAMESPACE = "pr-replay-eval"

MODEL = "llama-3.3-70b-versatile"
JUDGE_MODEL = "llama-3.3-70b-versatile"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

CORPUS_PAGES = 10  # fetcher hard-caps at 10 pages x 30 PRs
N_RETRIEVAL = 5
MAX_TOOL_ROUNDS = 2
# The model emits several tool calls per message, and each result stays in the
# conversation for every later turn. Without a total cap one task can assemble a
# prompt larger than the whole per-minute token allowance.
MAX_SEARCHES = 3

# Groq's on-demand tier allows 12k tokens/minute, and every retrieval round
# stays in the conversation for the rest of the generation. These caps keep a
# full task inside the window; raising them means raising --cooldown too.
MAX_SNIPPET_CHARS = 700
MAX_CONTEXT_CHARS = 3200
MAX_PATCH_CHARS = 3000
MAX_DIFF_CHARS = 4000

BENCH_DIR = os.path.join(os.path.dirname(__file__), "..", "benchmarks")
CORPUS_PATH = os.path.join(BENCH_DIR, "replay_corpus_langchain.json")
EXCLUSIONS_PATH = os.path.join(BENCH_DIR, "replay_exclusions_langchain.json")

# ---------------------------------------------------------------------------
# Held-out tasks
#
# Produced by select_tasks() over the corpus fetched 2026-07-28, then pinned so
# results stay reproducible as langchain moves. Re-derive with --select.
# ---------------------------------------------------------------------------

HELD_OUT_PRS: list[int] = [
    39101,  # fix(anthropic): strip unsupported fields from system message content blocks
    39021,  # fix(anthropic): enable structured output for Claude Opus 4.8
    39020,  # fix(core): use tool_call_schema cache for BaseTool token counting
    39009,  # fix(openai): correct gpt-5.3-chat-latest profile
    38845,  # fix(langchain): only retry retryable exceptions in ToolRetryMiddleware
    38786,  # feat(langchain): add meta extra and support langchain-meta in init_chat_model
    38781,  # feat(langchain): ToolErrorMiddleware
    38765,  # fix(anthropic): preserve empty thinking field in signature_delta streaming
    38751,  # fix(fireworks): report cached prompt token usage
    38686,  # fix(anthropic): add advisor_ prefix to builtin tool recognition
]

# Automated or mechanical PRs encode no engineering decision worth replaying.
_SKIP_TITLE = re.compile(r"^(chore\(model-profiles\)|release\(|bump |chore\(deps)", re.I)

_ISSUE_REF = re.compile(r"(?:fixes|closes|resolves)\s+#(\d+)", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _issue_refs(pr: dict) -> set[str]:
    return set(_ISSUE_REF.findall(pr.get("body") or ""))


def _first_commit(pr: dict) -> str:
    commits = pr.get("commits") or []
    return _norm(commits[0]["message"].splitlines()[0]) if commits else ""


def _paths(pr: dict) -> frozenset[str]:
    return frozenset(f["path"] for f in (pr.get("files") or []))


def answer_key_prs(corpus: list[dict], held_out: set[int]) -> dict[int, list[tuple[int, str]]]:
    """Find PRs that are a duplicate *attempt at the same fix* as a held-out PR.

    langchain contributors routinely re-file: a PR is opened, closed, and the
    same change lands under a new number. The closed twin sits in the corpus
    carrying the answer — same issue reference, same commit message, sometimes
    a near-identical title over the same files.

    Leaving those in does not test retrieval, it tests whether the model can
    copy. They are excluded from the index and reported, so the headline number
    is not quietly inflated by four lucky lookups.

    Returns {held_out_pr: [(duplicate_pr, reason), ...]}.
    """
    by_target: dict[int, list[tuple[int, str]]] = {}
    targets = [p for p in corpus if p["number"] in held_out]

    for target in targets:
        t_issues = _issue_refs(target)
        t_commit = _first_commit(target)
        t_paths = _paths(target)
        t_title = _norm(target["title"])
        found: list[tuple[int, str]] = []

        for other in corpus:
            number = other["number"]
            if number == target["number"] or number in held_out:
                continue
            shared = t_issues & _issue_refs(other)
            if shared:
                found.append((number, f"same issue ref #{sorted(shared)[0]}"))
                continue
            other_commit = _first_commit(other)
            if t_commit and other_commit == t_commit:
                found.append((number, "identical first commit message"))
                continue
            ratio = difflib.SequenceMatcher(None, t_title, _norm(other["title"])).ratio()
            if ratio > 0.8 and (_paths(other) & t_paths):
                found.append((number, f"title similarity {ratio:.2f} over shared files"))

        if found:
            by_target[target["number"]] = found
    return by_target


def select_tasks(prs: list[dict], limit: int = 10) -> list[dict]:
    """Pick merged PRs that state a problem and make a tractable code change."""
    merged = sorted(
        (p for p in prs if p.get("state") == "MERGED"), key=lambda p: -p["number"]
    )
    picked = []
    for pr in merged:
        if _SKIP_TITLE.search(pr["title"]):
            continue
        paths = [f["path"] for f in (pr.get("files") or [])]
        churn = pr.get("additions", 0) + pr.get("deletions", 0)
        body = (pr.get("body") or "").strip()
        if not any(p.endswith(".py") for p in paths):
            continue
        # Floor drops one-line constant swaps (no design decision to recover);
        # ceiling keeps the task and the ground-truth diff inside a judge prompt.
        if not (8 <= churn <= 400 and 1 <= len(paths) <= 6):
            continue
        if len(body) < 60:
            continue
        if all("data/_profiles" in p or p.endswith((".lock", ".toml")) for p in paths):
            continue
        picked.append(pr)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# Stage 1 — corpus
# ---------------------------------------------------------------------------


async def fetch_corpus() -> list[dict]:
    from fetcher.client import fetch_prs

    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN required to fetch the corpus")

    owner, name = REPO.split("/")
    prs = await fetch_prs(owner, name, pages=CORPUS_PAGES, github_token=token)

    os.makedirs(BENCH_DIR, exist_ok=True)
    with open(CORPUS_PATH, "w", encoding="utf-8") as f:
        json.dump(prs, f)

    merged = sum(1 for p in prs if p.get("state") == "MERGED")
    print(f"[+] {len(prs)} PRs ({merged} merged) -> {CORPUS_PATH}")
    return prs


def load_corpus() -> list[dict]:
    if not os.path.exists(CORPUS_PATH):
        raise SystemExit(f"No corpus at {CORPUS_PATH}. Run with --fetch first.")
    with open(CORPUS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Stage 2 — index everything except the held-out PRs
# ---------------------------------------------------------------------------


def build_index() -> None:
    from storage import delete_repo_index, index_prs

    corpus = load_corpus()
    held = set(HELD_OUT_PRS)

    missing = held - {p["number"] for p in corpus}
    if missing:
        raise SystemExit(
            f"Held-out PRs absent from the corpus: {sorted(missing)}. "
            "Re-fetch, or re-pin HELD_OUT_PRS with --select."
        )

    duplicates = answer_key_prs(corpus, held)
    excluded: set[int] = set()
    if duplicates:
        print("[*] Excluding duplicate attempts at held-out fixes (answer keys):")
        for target, dupes in sorted(duplicates.items()):
            for number, reason in dupes:
                excluded.add(number)
                print(f"      #{number} -> duplicate of #{target} ({reason})")
    with open(EXCLUSIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"held_out": sorted(held), "answer_keys": {str(k): v for k, v in duplicates.items()}},
            f,
            indent=2,
        )

    context_prs = [p for p in corpus if p["number"] not in held and p["number"] not in excluded]

    # A stale document from a previous run is indistinguishable from a fresh
    # one at query time, so the namespace is rebuilt from empty every time.
    print(f"[*] Clearing namespace '{NAMESPACE}'...")
    cleared = delete_repo_index(REPO, storage="permanent", namespace=NAMESPACE)
    print(f"    {cleared}")

    print(
        f"[*] Indexing {len(context_prs)} PRs "
        f"(held out {len(held)}, answer keys removed {len(excluded)})..."
    )
    count = index_prs(REPO, context_prs, temporary=False, namespace=NAMESPACE)
    print(f"[+] {count} documents indexed into '{NAMESPACE}'")

    # Held-out PRs and their duplicates must be absent from the store itself,
    # not merely filtered at query time.
    from storage import query_similar

    banned = held | excluded
    leaked = set()
    for pr_num in sorted(banned):
        hits = query_similar(
            repo_key=REPO,
            query_text=f"pull request {pr_num}",
            n_results=50,
            temporary=False,
            namespace=NAMESPACE,
        )
        for hit in hits:
            meta = hit.get("metadata") or {}
            if meta.get("pr_number") in banned:
                leaked.add(meta["pr_number"])
    if leaked:
        raise SystemExit(f"LEAK: excluded PRs present in the index: {sorted(leaked)}")
    print(f"[+] Verified: none of the {len(banned)} excluded PRs are in the index")


# ---------------------------------------------------------------------------
# Retrieval, with the leakage guard
# ---------------------------------------------------------------------------


def retrieve(query: str, before_pr: int) -> tuple[str, list[int], int]:
    """Return (context_text, pr_numbers, leak_count) for PRs older than `before_pr`."""
    from storage import query_similar

    hits = query_similar(
        repo_key=REPO,
        query_text=query,
        n_results=N_RETRIEVAL,
        temporary=False,
        namespace=NAMESPACE,
        where={"pr_number": {"$lt": before_pr}},
    )
    if not hits:
        return "No relevant history found.", [], 0

    snippets, prs, leaks = [], [], 0
    budget = MAX_CONTEXT_CHARS
    for hit in hits:
        meta = hit.get("metadata") or {}
        pr = meta.get("pr_number")
        if isinstance(pr, int) and pr >= before_pr:
            leaks += 1
            continue
        text = (hit.get("text") or "").strip()
        if not text:
            continue
        if pr is not None and pr not in prs:
            prs.append(pr)
        snippet = f"[PR #{pr}] {text[:MAX_SNIPPET_CHARS]}"
        if len(snippet) > budget:
            break
        budget -= len(snippet)
        snippets.append(snippet)

    if not snippets:
        return "No relevant history found.", prs, leaks
    return "PAST PULL REQUEST HISTORY:\n" + "\n\n".join(snippets), prs, leaks


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

SYSTEM = (
    "You are a senior engineer contributing to the langchain-ai/langchain "
    "monorepo. You produce the smallest correct change that resolves the issue, "
    "matching the conventions already used in this codebase. "
    "Output only code, organised per file. No prose commentary."
)


def task_prompt(pr: dict) -> str:
    paths = [f["path"] for f in (pr.get("files") or [])]
    files = "\n".join(f"  - {p}" for p in paths)
    body = (pr.get("body") or "").strip()[:2500]
    return (
        f"Repository: {REPO}\n\n"
        f"Issue to resolve:\n{pr['title']}\n\n"
        f"{body}\n\n"
        f"The change must be confined to these files:\n{files}\n\n"
        "For each file, give the new or modified code (full function or class "
        "bodies, not a diff). Include any test you would add."
    )


TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "search_pr_history",
        "description": (
            "Search this repository's merged pull requests, review comments and "
            "commit messages for how similar changes were made before. Call this "
            "first to match existing conventions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for, e.g. 'streaming chunk usage metadata'",
                }
            },
            "required": ["query"],
        },
    },
}


_RETRY_AFTER = re.compile(r"try again in ([\d.]+)s", re.I)


async def complete(client, shrink=None, **kwargs):
    """One chat completion, handling Groq's two very different refusals.

    429 means the minute's allowance is spent — wait it out. 413 means this one
    request is bigger than the entire per-minute allowance, so no amount of
    waiting will ever let it through; it has to get smaller. Treating 413 as a
    429 burns six escalating sleeps and then dies, which is exactly what the
    first run of this benchmark did.
    """
    import openai

    for attempt in range(6):
        try:
            return await client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            body = str(getattr(exc, "message", "") or exc)
            if exc.status_code == 429:
                match = _RETRY_AFTER.search(body)
                delay = float(match.group(1)) + 2 if match else 20.0 * (attempt + 1)
                print(f"      429; waiting {delay:.0f}s (attempt {attempt + 1}/6)")
                await asyncio.sleep(delay)
                continue
            if exc.status_code == 413 and shrink is not None:
                kwargs = shrink(kwargs)
                print(f"      413 too large; shrank payload (attempt {attempt + 1}/6)")
                continue
            raise
    raise RuntimeError("giving up after repeated rate-limit responses")


def _shrink_history(kwargs: dict) -> dict:
    """Halve the largest tool result still in the conversation."""
    messages = list(kwargs["messages"])
    biggest, size = None, 0
    for i, message in enumerate(messages):
        if message.get("role") == "tool" and len(message.get("content") or "") > size:
            biggest, size = i, len(message["content"])
    if biggest is not None and size > 200:
        trimmed = messages[biggest]["content"][: size // 2]
        messages[biggest] = {**messages[biggest], "content": trimmed + "\n[truncated]"}
    else:
        kwargs["max_tokens"] = max(400, int(kwargs.get("max_tokens", 1400) * 0.6))
    kwargs["messages"] = messages
    return kwargs


async def generate_inline(client, pr: dict) -> dict:
    """Retrieve once, then answer in a single turn — same shape as the baseline.

    The tool-calling arm confounds two things: whether the retrieved history
    helps, and whether this model degrades over a multi-turn tool exchange. This
    arm holds the conversation shape fixed at exactly one turn, so a difference
    against the baseline is attributable to the context itself.
    """
    query = f"{pr['title']} {' '.join((pr.get('body') or '').split())[:300]}"
    context, prs, leaks = await asyncio.to_thread(retrieve, query, pr["number"])
    print(f"      inline retrieve -> {len(prs)} PRs")

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"{context}\n\n---\n\n{task_prompt(pr)}"},
    ]
    resp = await complete(
        client,
        shrink=_shrink_history,
        model=MODEL,
        max_tokens=1400,
        temperature=0.0,
        messages=messages,
    )
    msg = resp.choices[0].message
    usage = resp.usage
    return {
        "code": msg.content or "",
        "tokens_in": usage.prompt_tokens if usage else 0,
        "tokens_out": usage.completion_tokens if usage else 0,
        "tool_calls": 1,
        "retrieved_prs": prs,
        "queries": [query],
        "leaks": leaks,
    }


async def generate(client, pr: dict, use_mcp: bool) -> dict:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task_prompt(pr)},
    ]
    tokens_in = tokens_out = rounds = leaks = 0
    retrieved: list[int] = []
    queries: list[str] = []

    while True:
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": 1400,
            "temperature": 0.0,
            "messages": messages,
        }
        # Offer the tool only while the search budget lasts. Once it is spent the
        # request goes out with no tools at all, so the model cannot answer with
        # another tool call — an empty `content` here would be scored as a
        # non-answer and silently wreck the arm's score.
        if use_mcp and rounds < MAX_TOOL_ROUNDS and len(queries) < MAX_SEARCHES:
            kwargs["tools"] = [TOOL_SPEC]
            kwargs["tool_choice"] = "required" if rounds == 0 else "auto"

        resp = await complete(client, shrink=_shrink_history, **kwargs)
        if resp.usage:
            tokens_in += resp.usage.prompt_tokens
            tokens_out += resp.usage.completion_tokens

        msg = resp.choices[0].message
        if msg.tool_calls:
            rounds += 1
            messages.append(msg.model_dump(exclude_unset=True))
            for tc in msg.tool_calls:
                if tc.function.name != "search_pr_history":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"[no such tool: {tc.function.name}]",
                        }
                    )
                    continue
                # Every tool_call in the message still needs a reply or the
                # conversation is malformed, so over-budget ones are answered
                # rather than dropped.
                if len(queries) >= MAX_SEARCHES:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "[search budget exhausted; answer with what you have]",
                        }
                    )
                    continue
                try:
                    query = json.loads(tc.function.arguments).get("query", pr["title"])
                except (json.JSONDecodeError, TypeError):
                    query = pr["title"]
                queries.append(query)
                text, prs, leak = await asyncio.to_thread(retrieve, query, pr["number"])
                retrieved.extend(p for p in prs if p not in retrieved)
                leaks += leak
                print(f"      search_pr_history({query!r}) -> {len(prs)} PRs")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})

            if rounds >= MAX_TOOL_ROUNDS or len(queries) >= MAX_SEARCHES:
                messages.append(
                    {
                        "role": "user",
                        "content": "Now write the change. Code only, organised per file.",
                    }
                )
            await asyncio.sleep(2)
            continue

        code = msg.content or ""
        if not code.strip():
            print("      WARNING: model returned an empty answer")
        return {
            "code": code,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_calls": rounds,
            "retrieved_prs": retrieved,
            "queries": queries,
            "leaks": leaks,
        }


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


async def ground_truth_diff(pr_number: int) -> str:
    from fetcher.diff import fetch_pr_diff

    files = await fetch_pr_diff(f"{REPO}#{pr_number}", github_token=os.getenv("GITHUB_TOKEN"))
    parts = []
    for entry in files:
        parts.append(f"--- {entry['file']} ({entry['change_type']}) ---")
        parts.extend(entry["hunks"])
    text = "\n".join(parts)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... [diff truncated] ..."
    return text


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

JUDGE_TEMPLATE = """You are a staff engineer grading two attempts at a change that was
actually merged into {repo}. You have the real merged diff as ground truth.

TASK GIVEN TO BOTH ENGINEERS:
{task}

--- GROUND TRUTH (the diff that was actually merged) ---
{diff}

--- ATTEMPT A ---
{code_a}

--- ATTEMPT B ---
{code_b}

Grade each attempt independently against the ground truth. Be strict: an empty,
truncated, or non-code answer scores 0 everywhere.

  functional_match  0-10  Does it make the same functional change as the ground
                          truth? Same root cause addressed, same behaviour after.
                          Cosmetic differences do not matter; missing or wrong
                          logic does.
  convention_match  0-10  Does it use the same idioms, helpers, naming and error
                          handling the ground truth uses in this codebase?
  revision_rounds   0-3   Review cycles before this could merge. 0 = merge-ready,
                          3 = major rework.

Return ONLY valid JSON, no markdown fences:
{{"A": {{"functional_match": <int>, "convention_match": <int>, "revision_rounds": <int>, "reasoning": "<one sentence>"}},
 "B": {{"functional_match": <int>, "convention_match": <int>, "revision_rounds": <int>, "reasoning": "<one sentence>"}}}}"""


def _clamp(value: Any, low: int, high: int, default: int) -> float:
    try:
        return float(max(low, min(high, int(value))))
    except (TypeError, ValueError):
        return float(default)


async def _judge_once(client, task: str, diff: str, code_a: str, code_b: str) -> tuple[dict, int, int]:
    def build(scale: float) -> str:
        return JUDGE_TEMPLATE.format(
            repo=REPO,
            task=task,
            diff=diff[: int(MAX_DIFF_CHARS * scale)],
            code_a=(code_a or "[empty]")[: int(MAX_PATCH_CHARS * scale)],
            code_b=(code_b or "[empty]")[: int(MAX_PATCH_CHARS * scale)],
        )

    # Rebuild the prompt smaller rather than blindly halving it, so the rubric
    # and the JSON contract at the end survive intact.
    scale = {"value": 1.0}

    def shrink(kwargs: dict) -> dict:
        scale["value"] *= 0.6
        kwargs["messages"] = [{"role": "user", "content": build(scale["value"])}]
        return kwargs

    resp = await complete(
        client,
        shrink=shrink,
        model=JUDGE_MODEL,
        max_tokens=700,
        temperature=0.0,
        messages=[{"role": "user", "content": build(1.0)}],
    )
    text = resp.choices[0].message.content or ""
    try:
        raw = json.loads(text[text.index("{") : text.rindex("}") + 1])
        parsed = {
            side: {
                "functional_match": _clamp(raw[side].get("functional_match"), 0, 10, 0),
                "convention_match": _clamp(raw[side].get("convention_match"), 0, 10, 0),
                "revision_rounds": _clamp(raw[side].get("revision_rounds"), 0, 3, 3),
                "reasoning": str(raw[side].get("reasoning", ""))[:300],
            }
            for side in ("A", "B")
        }
    except Exception as exc:
        print(f"    WARNING: judge parse failure — {exc}; raw: {text[:200]}")
        parsed = {
            side: {
                "functional_match": 0.0,
                "convention_match": 0.0,
                "revision_rounds": 3.0,
                "reasoning": "parse error",
            }
            for side in ("A", "B")
        }
    usage = resp.usage
    return parsed, (usage.prompt_tokens if usage else 0), (usage.completion_tokens if usage else 0)


async def judge_blinded(client, task: str, diff: str, mcp_code: str, base_code: str):
    """Judge in both presentation orders and average, cancelling position bias."""
    first, in1, out1 = await _judge_once(client, task, diff, mcp_code, base_code)
    await asyncio.sleep(3)
    second, in2, out2 = await _judge_once(client, task, diff, base_code, mcp_code)

    pairs = {"mcp": (first["A"], second["B"]), "baseline": (first["B"], second["A"])}
    result = {}
    for arm, (v1, v2) in pairs.items():
        result[arm] = {
            metric: round((v1[metric] + v2[metric]) / 2, 2)
            for metric in ("functional_match", "convention_match", "revision_rounds")
        }
        result[arm]["reasoning"] = v1["reasoning"]
    # How far the two orders disagreed on functional match — a judge-noise floor.
    result["order_gap"] = round(
        abs(first["A"]["functional_match"] - second["B"]["functional_match"])
        + abs(first["B"]["functional_match"] - second["A"]["functional_match"]),
        2,
    )
    return result, in1 + in2, out1 + out2


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def run(args) -> None:
    import openai

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise SystemExit("GROQ_API_KEY not set")

    corpus = {p["number"]: p for p in load_corpus()}
    targets = [corpus[n] for n in HELD_OUT_PRS if n in corpus][: args.limit]
    if not targets:
        raise SystemExit("No held-out PRs found in the corpus.")

    run_id = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    print(f"\nRun     : {run_id}\nRepo    : {REPO}\nModel   : {MODEL}\nTasks   : {len(targets)}\n")

    client = openai.AsyncOpenAI(base_url=GROQ_BASE_URL, api_key=api_key)
    rows: list[dict] = []

    for i, pr in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] PR #{pr['number']} — {pr['title'][:70]}")

        diff = await ground_truth_diff(pr["number"])

        print("    baseline (no retrieval)...")
        base = await generate(client, pr, use_mcp=False)
        await asyncio.sleep(args.pause)

        if args.arm == "inline":
            print("    mcp (context injected, single turn)...")
            mcp = await generate_inline(client, pr)
        else:
            print("    mcp (search_pr_history tool loop)...")
            mcp = await generate(client, pr, use_mcp=True)
        await asyncio.sleep(args.pause)

        print("    judging (blinded, both orders)...")
        verdict, j_in, j_out = await judge_blinded(
            client, task_prompt(pr), diff, mcp["code"], base["code"]
        )

        b, m = verdict["baseline"], verdict["mcp"]
        rows.append(
            {
                "run_id": run_id,
                "pr_number": pr["number"],
                "title": pr["title"],
                "base_functional": b["functional_match"],
                "mcp_functional": m["functional_match"],
                "functional_delta": round(m["functional_match"] - b["functional_match"], 2),
                "base_convention": b["convention_match"],
                "mcp_convention": m["convention_match"],
                "convention_delta": round(m["convention_match"] - b["convention_match"], 2),
                "base_revisions": b["revision_rounds"],
                "mcp_revisions": m["revision_rounds"],
                "revision_delta": round(b["revision_rounds"] - m["revision_rounds"], 2),
                "base_tokens": base["tokens_in"] + base["tokens_out"],
                "mcp_tokens": mcp["tokens_in"] + mcp["tokens_out"],
                "token_overhead": (mcp["tokens_in"] + mcp["tokens_out"])
                - (base["tokens_in"] + base["tokens_out"]),
                "base_empty": int(not base["code"].strip()),
                "mcp_empty": int(not mcp["code"].strip()),
                "retrieved_prs": json.dumps(mcp["retrieved_prs"]),
                "n_retrieved": len(mcp["retrieved_prs"]),
                "search_queries": json.dumps(mcp["queries"]),
                "leaks": mcp["leaks"],
                "order_gap": verdict["order_gap"],
                "judge_tokens": j_in + j_out,
                "base_reasoning": b["reasoning"],
                "mcp_reasoning": m["reasoning"],
            }
        )
        r = rows[-1]
        print(
            f"    functional base={r['base_functional']:.1f} mcp={r['mcp_functional']:.1f} "
            f"(D {r['functional_delta']:+.1f}) | convention base={r['base_convention']:.1f} "
            f"mcp={r['mcp_convention']:.1f} (D {r['convention_delta']:+.1f}) | "
            f"rounds D {r['revision_delta']:+.1f} | ctx {r['n_retrieved']} PRs\n"
        )

        if i < len(targets):
            await asyncio.sleep(args.cooldown)

    write_output(run_id, rows, args.arm)


def write_output(run_id: str, rows: list[dict], arm: str = "tool") -> None:
    os.makedirs(BENCH_DIR, exist_ok=True)
    csv_path = os.path.join(BENCH_DIR, f"replay_results_{arm}_{run_id}.csv")
    json_path = os.path.join(BENCH_DIR, f"replay_summary_{arm}_{run_id}.json")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    def avg(key: str) -> float:
        return round(sum(r[key] for r in rows) / len(rows), 2)

    wins = sum(1 for r in rows if r["functional_delta"] > 0)
    losses = sum(1 for r in rows if r["functional_delta"] < 0)

    summary = {
        "run_id": run_id,
        "repo": REPO,
        "arm": arm,
        "model": MODEL,
        "timestamp_utc": datetime.datetime.utcnow().isoformat(),
        "n_tasks": len(rows),
        "held_out_prs": [r["pr_number"] for r in rows],
        "leakage": {
            "held_out_prs_indexed": 0,
            "retrieval_violations": sum(r["leaks"] for r in rows),
            "note": "retrieval hard-filtered to pr_number < target; held-out PRs never indexed",
        },
        "functional_match": {
            "baseline": avg("base_functional"),
            "mcp": avg("mcp_functional"),
            "delta": avg("functional_delta"),
        },
        "convention_match": {
            "baseline": avg("base_convention"),
            "mcp": avg("mcp_convention"),
            "delta": avg("convention_delta"),
        },
        "revision_rounds": {
            "baseline": avg("base_revisions"),
            "mcp": avg("mcp_revisions"),
            "delta": avg("revision_delta"),
        },
        "tokens": {
            "baseline": avg("base_tokens"),
            "mcp": avg("mcp_tokens"),
            "overhead": avg("token_overhead"),
        },
        "record": {"mcp_wins": wins, "baseline_wins": losses, "ties": len(rows) - wins - losses},
        "empty_answers": {
            "baseline": sum(r["base_empty"] for r in rows),
            "mcp": sum(r["mcp_empty"] for r in rows),
            "note": "an empty answer scores 0 everywhere; any non-zero count invalidates that arm's mean",
        },
        "judge_noise": {
            "mean_order_gap": avg("order_gap"),
            "note": "mean absolute disagreement between the two presentation orders; "
            "treat deltas smaller than this as noise",
        },
        "csv": csv_path,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    fm, cm, rv, tk = (
        summary["functional_match"],
        summary["convention_match"],
        summary["revision_rounds"],
        summary["tokens"],
    )
    print("=" * 66)
    print(f"PR REPLAY BENCHMARK — {REPO} — {len(rows)} merged PRs — arm={arm}")
    print("=" * 66)
    print(f"  Functional match   baseline {fm['baseline']:5.2f}/10   mcp {fm['mcp']:5.2f}/10   D {fm['delta']:+.2f}")
    print(f"  Convention match   baseline {cm['baseline']:5.2f}/10   mcp {cm['mcp']:5.2f}/10   D {cm['delta']:+.2f}")
    print(f"  Revision rounds    baseline {rv['baseline']:5.2f}      mcp {rv['mcp']:5.2f}      D {rv['delta']:+.2f} fewer")
    print(f"  Tokens             baseline {tk['baseline']:7.0f}    mcp {tk['mcp']:7.0f}    overhead {tk['overhead']:+.0f}")
    print(f"  Record (functional) mcp {summary['record']['mcp_wins']}W "
          f"{summary['record']['baseline_wins']}L {summary['record']['ties']}T")
    print(f"  Judge noise floor  {summary['judge_noise']['mean_order_gap']:.2f}")
    print(f"  Retrieval leaks    {summary['leakage']['retrieval_violations']}")
    print(f"  Empty answers      baseline {summary['empty_answers']['baseline']}  mcp {summary['empty_answers']['mcp']}")
    print(f"\n  {csv_path}\n  {json_path}")
    print("=" * 66)


# ---------------------------------------------------------------------------
# Claude arm — the model is driven by subagents, not this script
#
# The Groq path above calls an API, so one process can run the whole loop. When
# the coding agent is Claude in the IDE, generation and judging happen in
# subagents instead. This script therefore only does the deterministic parts:
# it lays out the task, context and ground-truth files, and later reads the
# verdicts back and does the arithmetic. Nothing in between is the model's
# score to compute about itself.
#
# Budgets are far larger than the Groq path because there is no 12k-token
# minute limit here — notably the ground-truth diff is no longer truncated to
# 4000 chars, which had been cutting off 6 of the 10 targets.
# ---------------------------------------------------------------------------

CLAUDE_DIR = os.path.join(BENCH_DIR, "claude_run")
CLAUDE_N_RETRIEVAL = 10
CLAUDE_SNIPPET_CHARS = 1400
CLAUDE_CONTEXT_CHARS = 16000
CLAUDE_DIFF_CHARS = 24000


def _claude_retrieve(pr: dict) -> tuple[str, list[int], int]:
    """Same guard as the Groq path, with budgets sized for a large context."""
    from storage import query_similar

    query = f"{pr['title']} {' '.join((pr.get('body') or '').split())[:400]}"
    hits = query_similar(
        repo_key=REPO,
        query_text=query,
        n_results=CLAUDE_N_RETRIEVAL,
        temporary=False,
        namespace=NAMESPACE,
        where={"pr_number": {"$lt": pr["number"]}},
    )

    snippets, prs, leaks = [], [], 0
    budget = CLAUDE_CONTEXT_CHARS
    for hit in hits:
        meta = hit.get("metadata") or {}
        number = meta.get("pr_number")
        if isinstance(number, int) and number >= pr["number"]:
            leaks += 1
            continue
        text = (hit.get("text") or "").strip()
        if not text:
            continue
        if number is not None and number not in prs:
            prs.append(number)
        snippet = f"### From PR #{number}\n{text[:CLAUDE_SNIPPET_CHARS]}"
        if len(snippet) > budget:
            break
        budget -= len(snippet)
        snippets.append(snippet)
    return "\n\n".join(snippets), prs, leaks


async def prepare_claude(limit: int) -> None:
    corpus = {p["number"]: p for p in load_corpus()}
    targets = [corpus[n] for n in HELD_OUT_PRS if n in corpus][:limit]
    os.makedirs(CLAUDE_DIR, exist_ok=True)

    manifest = []
    for pr in targets:
        number = pr["number"]
        task = task_prompt(pr)
        context, prs, leaks = _claude_retrieve(pr)
        diff = await ground_truth_diff_full(number)

        # Each arm gets its own directory holding exactly what that arm is
        # entitled to see. The baseline solver has no path to the history file,
        # and neither solver has a path to the ground truth, which lives in a
        # separate tree the solvers are never pointed at.
        base_dir = os.path.join(CLAUDE_DIR, "tasks", str(number), "base")
        mcp_dir = os.path.join(CLAUDE_DIR, "tasks", str(number), "mcp")
        for directory in (base_dir, mcp_dir):
            os.makedirs(directory, exist_ok=True)
        _write_at(os.path.join(base_dir, "task.md"), task)
        _write_at(os.path.join(mcp_dir, "task.md"), task)
        _write_at(os.path.join(mcp_dir, "history.md"), context or "No relevant history found.")

        truth_dir = os.path.join(CLAUDE_DIR, "truth")
        os.makedirs(truth_dir, exist_ok=True)
        _write_at(os.path.join(truth_dir, f"{number}.diff"), diff)

        for sub in ("patches", "verdicts"):
            os.makedirs(os.path.join(CLAUDE_DIR, sub), exist_ok=True)

        manifest.append(
            {
                "pr_number": number,
                "title": pr["title"],
                "retrieved_prs": prs,
                "n_retrieved": len(prs),
                "leaks": leaks,
                "context_chars": len(context),
                "truth_chars": len(diff),
                "truth_truncated": diff.endswith("[diff truncated] ..."),
            }
        )
        print(
            f"  #{number}: context {len(context):6d} chars from {len(prs):2d} PRs | "
            f"truth {len(diff):6d} chars{' (truncated)' if manifest[-1]['truth_truncated'] else ''}"
            f" | leaks {leaks}"
        )

    _write("manifest.json", json.dumps(manifest, indent=2))
    total_leaks = sum(m["leaks"] for m in manifest)
    print(f"\n[+] {len(manifest)} tasks prepared in {CLAUDE_DIR}")
    print(f"[+] retrieval leak violations: {total_leaks}")


def _write(name: str, text: str) -> None:
    _write_at(os.path.join(CLAUDE_DIR, name), text)


def _write_at(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


async def ground_truth_diff_full(pr_number: int) -> str:
    from fetcher.diff import fetch_pr_diff

    files = await fetch_pr_diff(f"{REPO}#{pr_number}", github_token=os.getenv("GITHUB_TOKEN"))
    parts = []
    for entry in files:
        parts.append(f"--- {entry['file']} ({entry['change_type']}) ---")
        parts.extend(entry["hunks"])
    text = "\n".join(parts)
    if len(text) > CLAUDE_DIFF_CHARS:
        text = text[:CLAUDE_DIFF_CHARS] + "\n... [diff truncated] ..."
    return text


def blind_claude() -> None:
    """Copy each arm's patch to a neutral A/B filename for judging.

    The judge is handed file paths. `39101_mcp.md` announces the answer, so the
    patches are re-emitted under names that carry no arm label. Order 1 puts the
    retrieval arm in slot A, order 2 swaps them; averaging the two verdicts is
    what cancels the judge's positional preference.
    """
    with open(os.path.join(CLAUDE_DIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)

    blind_dir = os.path.join(CLAUDE_DIR, "blind")
    os.makedirs(blind_dir, exist_ok=True)

    written = 0
    for entry in manifest:
        number = entry["pr_number"]
        sources = {
            arm: os.path.join(CLAUDE_DIR, "patches", f"{number}_{arm}.md")
            for arm in ("mcp", "base")
        }
        missing = [a for a, p in sources.items() if not os.path.exists(p)]
        if missing:
            print(f"  [!] #{number}: missing patch for {missing}; skipping")
            continue

        contents = {}
        for arm, path in sources.items():
            with open(path, encoding="utf-8") as f:
                contents[arm] = f.read()

        for order, (slot_a, slot_b) in {1: ("mcp", "base"), 2: ("base", "mcp")}.items():
            _write_at(os.path.join(blind_dir, f"{number}_{order}_A.md"), contents[slot_a])
            _write_at(os.path.join(blind_dir, f"{number}_{order}_B.md"), contents[slot_b])
        written += 1

    print(f"[+] blinded {written} task(s) into {blind_dir}")


def aggregate_claude() -> None:
    """Read subagent verdicts and compute the summary.

    Order 1 presents the MCP arm as A; order 2 swaps them. Averaging the two
    cancels whatever positional preference the judge has.
    """
    with open(os.path.join(CLAUDE_DIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)

    rows = []
    for entry in manifest:
        number = entry["pr_number"]
        verdicts = {}
        for order in (1, 2):
            path = os.path.join(CLAUDE_DIR, "verdicts", f"verdict_{number}_{order}.json")
            if not os.path.exists(path):
                print(f"  [!] missing verdict for #{number} order {order}; skipping task")
                verdicts = {}
                break
            with open(path, encoding="utf-8") as f:
                text = f.read()
            verdicts[order] = json.loads(text[text.index("{") : text.rindex("}") + 1])
        if not verdicts:
            continue

        # order 1: A=mcp, B=baseline.  order 2: A=baseline, B=mcp.
        pairs = {
            "mcp": (verdicts[1]["A"], verdicts[2]["B"]),
            "baseline": (verdicts[1]["B"], verdicts[2]["A"]),
        }
        scored = {}
        for arm, (v1, v2) in pairs.items():
            scored[arm] = {
                metric: round((float(v1[metric]) + float(v2[metric])) / 2, 2)
                for metric in ("functional_match", "convention_match", "revision_rounds")
            }
            scored[arm]["reasoning"] = v1.get("reasoning", "")

        b, m = scored["baseline"], scored["mcp"]
        order_gap = round(
            abs(float(verdicts[1]["A"]["functional_match"]) - float(verdicts[2]["B"]["functional_match"]))
            + abs(float(verdicts[1]["B"]["functional_match"]) - float(verdicts[2]["A"]["functional_match"])),
            2,
        )
        rows.append(
            {
                "run_id": "claude",
                "pr_number": number,
                "title": entry["title"],
                "base_functional": b["functional_match"],
                "mcp_functional": m["functional_match"],
                "functional_delta": round(m["functional_match"] - b["functional_match"], 2),
                "base_convention": b["convention_match"],
                "mcp_convention": m["convention_match"],
                "convention_delta": round(m["convention_match"] - b["convention_match"], 2),
                "base_revisions": b["revision_rounds"],
                "mcp_revisions": m["revision_rounds"],
                "revision_delta": round(b["revision_rounds"] - m["revision_rounds"], 2),
                "base_tokens": 0,
                "mcp_tokens": 0,
                "token_overhead": 0,
                "base_empty": 0,
                "mcp_empty": 0,
                "retrieved_prs": json.dumps(entry["retrieved_prs"]),
                "n_retrieved": entry["n_retrieved"],
                "search_queries": "[]",
                "leaks": entry["leaks"],
                "order_gap": order_gap,
                "judge_tokens": 0,
                "base_reasoning": b["reasoning"],
                "mcp_reasoning": m["reasoning"],
            }
        )

    if not rows:
        raise SystemExit("No verdicts found — run the judging subagents first.")
    write_output(datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S"), rows, "claude")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay merged PRs with and without PR-history retrieval")
    parser.add_argument("--fetch", action="store_true", help="fetch and cache the PR corpus")
    parser.add_argument("--index", action="store_true", help="index the corpus minus held-out PRs")
    parser.add_argument("--select", action="store_true", help="re-derive the held-out task list")
    parser.add_argument("--run", action="store_true", help="run the A/B benchmark")
    parser.add_argument("--prepare-claude", action="store_true",
                        help="lay out task/context/ground-truth files for the Claude subagent arm")
    parser.add_argument("--blind-claude", action="store_true",
                        help="re-emit patches under neutral A/B names for blinded judging")
    parser.add_argument("--aggregate-claude", action="store_true",
                        help="read subagent verdicts and write the Claude-arm summary")
    parser.add_argument("--limit", type=int, default=10, help="number of tasks to run")
    parser.add_argument(
        "--arm",
        choices=("tool", "inline"),
        default="tool",
        help="how the retrieval arm receives context: an MCP-style tool loop "
        "(default, matches real usage) or injected into a single-turn prompt "
        "(controls for multi-turn degradation)",
    )
    parser.add_argument("--pause", type=float, default=3, help="seconds between API calls")
    parser.add_argument("--cooldown", type=float, default=30, help="seconds between tasks")
    args = parser.parse_args()

    if args.fetch:
        asyncio.run(fetch_corpus())
    if args.select:
        for pr in select_tasks(load_corpus()):
            print(f"    {pr['number']},  # {pr['title'][:80]}")
    if args.index:
        build_index()
    if args.run:
        asyncio.run(run(args))
    if args.prepare_claude:
        asyncio.run(prepare_claude(args.limit))
    if args.blind_claude:
        blind_claude()
    if args.aggregate_claude:
        aggregate_claude()
    if not any((args.fetch, args.index, args.select, args.run, args.prepare_claude,
                args.blind_claude, args.aggregate_claude)):
        parser.print_help()


if __name__ == "__main__":
    main()
