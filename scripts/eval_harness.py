"""
scripts/eval_harness.py

Benchmarks the PR Context MCP against a no-context baseline across three metrics:
  1. Revision Round Reduction   — estimated review cycles before merge
  2. Net Token Cost             — full pipeline cost including retrieval overhead
  3. Pattern Adherence Rate     — repo-specific rule compliance (0-10)

Golden tasks target psf/black formatter behaviors. Each task carries
"known_patterns" — review-enforced requirements the judge scores per-pattern
(binary followed/not-followed, judged in both A/B orders to cancel position bias).

Usage:
    pip install openai python-dotenv
    export GROQ_API_KEY=gsk-...
    python scripts/eval_harness.py [--repo psf/black] [--dry-run]

Output:
    benchmarks/eval_results_<run_id>.csv
    benchmarks/eval_summary_<run_id>.json
"""

import os
import sys
import json
import csv
import uuid
import random
import asyncio
import argparse
import datetime
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import openai
except ImportError:
    print("ERROR: pip install openai")
    sys.exit(1)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from storage import query_similar
    _HAS_STORAGE = True
except ImportError:
    _HAS_STORAGE = False
    print("WARNING: Could not import storage.query_similar — index Tracer-Cloud/opensre first.")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL          = "llama-3.3-70b-versatile"
JUDGE_MODEL    = "llama-3.3-70b-versatile"
MIN_TASKS      = 5
N_RETRIEVAL    = 10
GROQ_BASE_URL  = "https://api.groq.com/openai/v1"

# ---------------------------------------------------------------------------
# Golden Tasks — psf/black formatter behaviors with review-enforced patterns
#
# To populate retrieval_ground_truth:
#   Index the repo, run a query, note which PR numbers surface, then manually
#   verify those PRs actually contain the relevant pattern. Add their numbers here.
#   Until populated, recall@k reports "no_ground_truth".
# ---------------------------------------------------------------------------

GOLDEN_TASKS: List[Dict] = [
    {
        "task_id": "quote-normalization",
        "repo": "psf/black",
        "prompt": (
            "Write a black formatter function that normalizes string quotes. "
            "It should convert double-quoted strings to single-quoted where possible, "
            "and handle edge cases like strings containing the target quote character."
        ),
        "known_patterns": [
            "Must not alter strings that contain the target quote character inside them",
            "Raw strings and byte strings must be handled separately from regular strings",
            "Backslash handling inside strings must be validated before normalizing",
            "The transformation must be idempotent — formatting twice gives the same result",
            "Must preserve string prefix (r, b, f, rb) exactly as-is after normalization",
        ],
        "retrieval_ground_truth": [],
    },
    {
        "task_id": "magic-trailing-comma",
        "repo": "psf/black",
        "prompt": (
            "Implement the magic trailing comma feature in black. "
            "When a trailing comma is present in a collection, black should not collapse "
            "it to a single line even if it would fit within the line length limit."
        ),
        "known_patterns": [
            "A trailing comma in a collection must always force exploded formatting",
            "Magic trailing comma must be respected even when the collection fits on one line",
            "The feature must be detectable from the AST node, not from string scanning",
            "Removing the trailing comma should allow black to collapse the collection",
            "Must apply to function arguments, function parameters, imports, and collections",
        ],
        "retrieval_ground_truth": [],
    },
    {
        "task_id": "target-version-compatibility",
        "repo": "psf/black",
        "prompt": (
            "Add support for a new --target-version flag in black that lets users specify "
            "the minimum Python version to target. Formatting decisions that depend on "
            "Python version (like f-strings, walrus operator) should respect this flag."
        ),
        "known_patterns": [
            "Target version must be an enum, not a raw string or integer",
            "Multiple target versions can be specified simultaneously",
            "Version-specific syntax must only be used if all target versions support it",
            "The flag must be documented with clear examples in the CLI help text",
            "Default behavior with no flag must be identical to previous black behavior",
        ],
        "retrieval_ground_truth": [],
    },
    {
        "task_id": "comment-preservation",
        "repo": "psf/black",
        "prompt": (
            "Fix a bug in black where a comment following an escaped newline causes "
            "unstable formatting — running black twice produces different output. "
            "The formatter must produce identical output on repeated runs."
        ),
        "known_patterns": [
            "Bug fix must include a regression test with the exact input that caused instability",
            "The fix must not alter comment content — only whitespace around it",
            "Idempotency must be verified: format the output again and assert no change",
            "Minimal change — only touch the code path responsible for the unstable case",
            "The test input must be added to the black test corpus as a data file",
        ],
        "retrieval_ground_truth": [],
    },
    {
        "task_id": "parentheses-unwrapping",
        "repo": "psf/black",
        "prompt": (
            "Fix unstable formatting in black involving multiple nested parentheses. "
            "Black should unwrap redundant parentheses in a single pass "
            "without requiring multiple formatting runs to stabilize."
        ),
        "known_patterns": [
            "Redundant parentheses around return values must be removed",
            "Parentheses that are load-bearing (tuple, generator) must never be removed",
            "The fix must handle multiple levels of nesting in a single pass",
            "Idempotency test must be included: format twice, assert output is identical",
            "Must not remove parentheses when they affect operator precedence",
        ],
        "retrieval_ground_truth": [],
    },
    {
        "task_id": "trailing-comma-import",
        "repo": "psf/black",
        "prompt": (
            "Implement the rule that black adds a trailing comma to a single import "
            "that does not fit on one line when wrapped in parentheses. "
            "For example: from module import (very_long_name,)"
        ),
        "known_patterns": [
            "Trailing comma must only be added when the import is wrapped to multiple lines",
            "Single-line imports that fit within the limit must not be modified",
            "The added comma must trigger magic trailing comma behavior on next format",
            "Must handle star imports and aliased imports without adding trailing comma",
            "Regression test must cover the single-import edge case explicitly",
        ],
        "retrieval_ground_truth": [],
    },
    {
        "task_id": "blank-lines-class",
        "repo": "psf/black",
        "prompt": (
            "Implement the black rule that adds a blank line after a nontrivial class body "
            "before the first method. A nontrivial class is one that has a docstring "
            "or class-level variable assignments before its methods."
        ),
        "known_patterns": [
            "Blank line must be added after docstring or class variable, before first method",
            "Empty classes and classes with only pass must not get a blank line added",
            "The rule must not add more than one blank line even if multiple assignments exist",
            "Nested classes must apply the same rule recursively",
            "Test must cover trivial class, class with docstring, and class with assignments",
        ],
        "retrieval_ground_truth": [],
    },
    {
        "task_id": "pyi-stub-file-support",
        "repo": "psf/black",
        "prompt": (
            "Add support for formatting .pyi stub files in black. "
            "Stub files have different formatting rules: they allow more condensed "
            "formatting with fewer blank lines between top-level definitions."
        ),
        "known_patterns": [
            "Stub file mode must be activated by file extension .pyi, not a flag",
            "Blank line rules between top-level definitions differ from regular Python files",
            "Stub files allow single-line function bodies for simple stubs",
            "The mode must be clearly documented with before/after examples",
            "All existing non-stub formatting behavior must be completely unaffected",
        ],
        "retrieval_ground_truth": [],
    },
]


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

async def fetch_mcp_context(repo: str, prompt: str) -> Tuple[str, List[int]]:
    if not _HAS_STORAGE:
        raise RuntimeError(
            "storage module not importable. "
            "Index the repo first via the MCP server's index_repository tool on Tracer-Cloud/opensre"
        )

    results = await asyncio.to_thread(
        query_similar,
        repo_key=repo,
        query_text=prompt,
        n_results=N_RETRIEVAL,
        temporary=False,
        namespace=None,
    )

    if not results:
        return "No relevant historical context found for this repository.", []

    # query_similar returns {"text": ..., "metadata": ..., "similarity": ...}
    snippets: List[str] = []
    pr_numbers: List[int] = []
    seen_prs: set = set()
    for r in results:
        doc = (r.get("text") or "").strip()
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        pr = meta.get("pr_number")
        if pr is not None and pr not in seen_prs:
            seen_prs.add(pr)
            pr_numbers.append(pr)
        if doc:
            tag = f"[PR #{pr}]" if pr is not None else "-"
            snippets.append(f"{tag} {doc}")

    if not snippets:
        return "No relevant historical context found for this repository.", pr_numbers

    return "HISTORICAL PR REVIEW CONTEXT:\n" + "\n\n".join(snippets), pr_numbers


def recall_at_k(retrieved: List[int], ground_truth: List[int], k: int = 3) -> Optional[float]:
    if not ground_truth:
        return None
    return len(set(retrieved[:k]) & set(ground_truth)) / len(set(ground_truth))


# ---------------------------------------------------------------------------
# Generation — agentic loop
# ---------------------------------------------------------------------------

async def generate_code(
    client: openai.AsyncOpenAI,
    prompt: str,
    repo: str,
    use_mcp: bool = False,
) -> Tuple[str, int, int, int, List[int]]:
    """Returns (code, in_tokens, out_tokens, context_tokens, retrieved_pr_numbers)."""

    system = (
        "You are a senior software engineer contributing to an open-source Python project. "
        "Write clean, production-ready code with full type hints. "
        "Return code only — no prose explanation."
    )
    messages: List[Dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    tools = []
    if use_mcp:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "semantic_search_reviews",
                    "description": (
                        "Search past PR review comments to understand team coding standards "
                        "before writing code. Call this FIRST to avoid common review nitpicks."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "What to search for (e.g. 'tool decorator pattern', 'error handling')",
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    total_in = total_out = context_tokens = 0
    all_retrieved_prs: List[int] = []
    tool_calls_made = 0

    while True:
        kwargs: Dict = {
            "model": MODEL,
            "max_tokens": 1500,
            "temperature": 0.0,
            "messages": messages,
        }
        if use_mcp:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "required" if tool_calls_made == 0 else "auto"

        response = await client.chat.completions.create(**kwargs)

        if response.usage:
            total_in  += response.usage.prompt_tokens
            total_out += response.usage.completion_tokens

        message = response.choices[0].message

        if message.tool_calls and tool_calls_made < 2:
            tool_calls_made += 1
            messages.append(message.model_dump(exclude_unset=True))
            for tc in message.tool_calls:
                if tc.function.name == "semantic_search_reviews":
                    args  = json.loads(tc.function.arguments)
                    query = args.get("query", prompt[:80])
                    print(f"    [Agent] semantic_search_reviews(query='{query}')")
                    try:
                        ctx_text, prs = await fetch_mcp_context(repo, query)
                        all_retrieved_prs.extend(prs)
                        context_tokens += len(ctx_text) // 4
                    except RuntimeError as e:
                        ctx_text = f"[Tool error: {e}]"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": ctx_text,
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"[Error: tool '{tc.function.name}' not found]",
                    })
        else:
            return (
                message.content or "",
                total_in,
                total_out,
                context_tokens,
                all_retrieved_prs,
            )


# ---------------------------------------------------------------------------
# Judge — blinded
# ---------------------------------------------------------------------------

async def _judge_once(
    client: openai.AsyncOpenAI,
    task: Dict,
    code_a: str,
    code_b: str,
) -> Tuple[Dict, int, int]:
    """One blinded judging pass. Returns per-implementation pattern booleans."""
    patterns = task["known_patterns"]
    numbered = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(patterns))

    judge_prompt = f"""You are a Staff Engineer reviewing two implementations submitted to the {task['repo']} project.

Task given to the developer:
{task['prompt']}

REQUIRED TEAM PATTERNS (enforced at review — non-negotiable):
{numbered}

--- IMPLEMENTATION A ---
{code_a}

--- IMPLEMENTATION B ---
{code_b}

For EACH implementation, judge EACH numbered pattern strictly: true only if the
code fully satisfies it. Partial compliance is false. An empty or non-code
response is false for every pattern.
Also estimate revision rounds before merge: 0=merge-ready, 1=minor nits,
2=structural issues, 3=major rework.

Return ONLY valid JSON. No other text, no markdown fences:
{{
  "A": {{
    "pattern_followed": [<{len(patterns)} booleans, one per numbered pattern, in order>],
    "estimated_revision_rounds": <int 0-3>,
    "reasoning": "<one sentence>"
  }},
  "B": {{
    "pattern_followed": [<{len(patterns)} booleans, in order>],
    "estimated_revision_rounds": <int 0-3>,
    "reasoning": "<one sentence>"
  }}
}}"""

    response = await client.chat.completions.create(
        model=JUDGE_MODEL,
        max_tokens=700,
        temperature=0.0,
        messages=[{"role": "user", "content": judge_prompt}],
    )

    text = response.choices[0].message.content or ""
    n = len(patterns)
    try:
        start = text.find("{")
        end   = text.rfind("}") + 1
        raw   = json.loads(text[start:end])
        for side in ("A", "B"):
            flags = raw[side].get("pattern_followed", [])
            flags = [bool(f) for f in flags][:n]
            flags += [False] * (n - len(flags))
            raw[side]["pattern_followed"] = flags
    except Exception as exc:
        print(f"  WARNING: Judge parse error — {exc}")
        print(f"  Raw output: {text[:300]}")
        raw = {
            side: {"pattern_followed": [False] * n, "estimated_revision_rounds": 3, "reasoning": "parse error"}
            for side in ("A", "B")
        }

    in_tok  = response.usage.prompt_tokens     if response.usage else 0
    out_tok = response.usage.completion_tokens if response.usage else 0
    return raw, in_tok, out_tok


async def judge_blinded(
    client: openai.AsyncOpenAI,
    task: Dict,
    mcp_code: str,
    base_code: str,
) -> Tuple[Dict, int, int]:
    """Judge in BOTH presentation orders and average, cancelling position bias.

    Returns ({"mcp": {...}, "baseline": {...}}, in_tokens, out_tokens).
    pattern_adherence_score is 0-10 (fraction of patterns followed x 10, averaged
    over both orders). patterns_missed = patterns judged false in both orders.
    """
    patterns = task["known_patterns"]

    raw1, in1, out1 = await _judge_once(client, task, code_a=mcp_code, code_b=base_code)
    raw2, in2, out2 = await _judge_once(client, task, code_a=base_code, code_b=mcp_code)

    verdicts = {
        "mcp":      (raw1["A"], raw2["B"]),
        "baseline": (raw1["B"], raw2["A"]),
    }

    result: Dict = {}
    disagreements = 0
    for arm, (v1, v2) in verdicts.items():
        f1, f2 = v1["pattern_followed"], v2["pattern_followed"]
        disagreements += sum(1 for a, b in zip(f1, f2) if a != b)
        score = 10.0 * (sum(f1) + sum(f2)) / (2 * len(patterns))
        missed = [p for p, a, b in zip(patterns, f1, f2) if not a and not b]
        rounds = (
            float(v1.get("estimated_revision_rounds", 3)) +
            float(v2.get("estimated_revision_rounds", 3))
        ) / 2
        result[arm] = {
            "pattern_adherence_score": round(score, 2),
            "estimated_revision_rounds": rounds,
            "patterns_missed": missed,
            "reasoning": v1.get("reasoning", ""),
        }
    result["order_disagreements"] = disagreements

    return result, in1 + in2, out1 + out2


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_benchmark(args: argparse.Namespace) -> None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY not set in .env or environment")
        sys.exit(1)

    tasks = GOLDEN_TASKS
    if args.repo:
        tasks = [t for t in tasks if t["repo"] == args.repo]
        if not tasks:
            print(f"ERROR: No tasks for repo '{args.repo}'")
            sys.exit(1)

    if len(tasks) < MIN_TASKS:
        print(f"WARNING: Only {len(tasks)} tasks. Need {MIN_TASKS}+ for meaningful results.")

    run_id = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:6]
    print(f"\nRun ID : {run_id}")
    print(f"Model  : {MODEL}")
    print(f"Tasks  : {len(tasks)}")
    if args.dry_run:
        print("DRY RUN — no API calls\n")

    client = openai.AsyncOpenAI(base_url=GROQ_BASE_URL, api_key=api_key)
    results_data: List[Dict] = []

    for i, task in enumerate(tasks):
        print(f"\n[{i+1}/{len(tasks)}] {task['task_id']}")

        retrieved_prs: List[int] = []
        context_tokens = 0

        if not args.dry_run:
            print("  → MCP agent (with semantic search tool)...")
            await asyncio.sleep(3)
            mcp_code, mcp_in, mcp_out, context_tokens, retrieved_prs = await generate_code(
                client, task["prompt"], repo=task["repo"], use_mcp=True
            )
            print(f"    Context: {context_tokens} tokens, {len(retrieved_prs)} PRs retrieved")
            if context_tokens < 50:
                print("    WARNING: retrieved context is nearly empty — is the repo indexed?")

            print("  → Baseline agent (no tools)...")
            await asyncio.sleep(3)
            base_code, base_in, base_out, _, _ = await generate_code(
                client, task["prompt"], repo=task["repo"], use_mcp=False
            )

            print("  → Judging (blinded, both orders)...")
            await asyncio.sleep(3)
            judge, j_in, j_out = await judge_blinded(client, task, mcp_code, base_code)
        else:
            mcp_code = base_code = "[DRY RUN]"
            mcp_in = mcp_out = base_in = base_out = j_in = j_out = 0
            judge = {
                "baseline": {"pattern_adherence_score": 0, "estimated_revision_rounds": 0, "patterns_missed": [], "reasoning": "dry run"},
                "mcp":      {"pattern_adherence_score": 0, "estimated_revision_rounds": 0, "patterns_missed": [], "reasoning": "dry run"},
                "order_disagreements": 0,
            }

        r3 = recall_at_k(retrieved_prs, task.get("retrieval_ground_truth", []), k=3)
        r5 = recall_at_k(retrieved_prs, task.get("retrieval_ground_truth", []), k=5)

        base_tokens = base_in + base_out
        # NOTE: retrieved context is already counted inside mcp_in (it enters the
        # follow-up call's prompt tokens) — do NOT add context_tokens again.
        mcp_tokens  = mcp_in + mcp_out

        row = {
            "run_id":               run_id,
            "task_id":              task["task_id"],
            "repo":                 task["repo"],
            "base_revisions":       judge["baseline"]["estimated_revision_rounds"],
            "mcp_revisions":        judge["mcp"]["estimated_revision_rounds"],
            "revision_delta":       judge["baseline"]["estimated_revision_rounds"] - judge["mcp"]["estimated_revision_rounds"],
            "base_tokens":          base_tokens,
            "mcp_context_tokens":   context_tokens,
            "mcp_gen_tokens":       mcp_in + mcp_out,
            "mcp_total_tokens":     mcp_tokens,
            "token_upfront_delta":  base_tokens - mcp_tokens,
            "base_score":           judge["baseline"]["pattern_adherence_score"],
            "mcp_score":            judge["mcp"]["pattern_adherence_score"],
            "score_delta":          judge["mcp"]["pattern_adherence_score"] - judge["baseline"]["pattern_adherence_score"],
            "recall_at_3":          r3 if r3 is not None else "no_ground_truth",
            "recall_at_5":          r5 if r5 is not None else "no_ground_truth",
            "retrieved_prs":        json.dumps(retrieved_prs),
            "base_patterns_missed": json.dumps(judge["baseline"].get("patterns_missed", [])),
            "mcp_patterns_missed":  json.dumps(judge["mcp"].get("patterns_missed", [])),
            "base_reasoning":       judge["baseline"]["reasoning"],
            "mcp_reasoning":        judge["mcp"]["reasoning"],
            "judge_tokens":         j_in + j_out,
            "order_disagreements":  judge.get("order_disagreements", 0),
        }
        results_data.append(row)

        print(f"  Revisions : base={row['base_revisions']}  mcp={row['mcp_revisions']}  Δ={row['revision_delta']:+.1f}")
        print(f"  Adherence : base={row['base_score']}/10  mcp={row['mcp_score']}/10  Δ={row['score_delta']:+.1f}")
        print(f"  Tokens    : base={row['base_tokens']}  mcp_total={row['mcp_total_tokens']} (ctx={context_tokens})")

        if i < len(tasks) - 1:
            print("  Waiting 60s for rate limit...")
            await asyncio.sleep(60)

    if not results_data:
        print("\nNo results collected.")
        return

    os.makedirs("benchmarks", exist_ok=True)
    csv_path  = f"benchmarks/eval_results_{run_id}.csv"
    json_path = f"benchmarks/eval_summary_{run_id}.json"

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=results_data[0].keys())
        writer.writeheader()
        writer.writerows(results_data)

    def avg(key: str) -> Optional[float]:
        vals = [r[key] for r in results_data if isinstance(r[key], (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    avg_rev_delta  = avg("revision_delta")
    est_savings    = round((avg_rev_delta or 0) * 3000, 0)

    summary = {
        "run_id":        run_id,
        "model":         MODEL,
        "timestamp_utc": datetime.datetime.utcnow().isoformat(),
        "n_tasks":       len(results_data),
        "repo":          tasks[0]["repo"] if tasks else "Unknown",
        "benchmarks": {
            "revision_round_reduction": {
                "avg_baseline": avg("base_revisions"),
                "avg_mcp":      avg("mcp_revisions"),
                "avg_delta":    avg_rev_delta,
                "headline":     f"MCP saves {avg_rev_delta:+.1f} revision rounds on average",
            },
            "token_cost": {
                "avg_baseline_tokens":    avg("base_tokens"),
                "avg_mcp_total_tokens":   avg("mcp_total_tokens"),
                "avg_context_overhead":   avg("mcp_context_tokens"),
                "avg_upfront_delta":      avg("token_upfront_delta"),
                "est_downstream_savings": est_savings,
                "note": (
                    "upfront_delta < 0 = MCP costs more tokens at generation time. "
                    "est_downstream_savings = avoided revision cycles * ~3000 tokens/cycle."
                ),
            },
            "pattern_adherence": {
                "avg_baseline": avg("base_score"),
                "avg_mcp":      avg("mcp_score"),
                "avg_delta":    avg("score_delta"),
                "headline":     f"MCP improves pattern adherence by {avg('score_delta'):+.1f}/10" if avg("score_delta") is not None else "N/A",
            },
        },
        "csv": csv_path,
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    b  = summary["benchmarks"]
    rv = b["revision_round_reduction"]
    tc = b["token_cost"]
    pa = b["pattern_adherence"]

    print("\n" + "=" * 62)
    print(f"BENCHMARK RESULTS  |  {run_id[:22]}")
    print("=" * 62)

    print(f"\n📊 Benchmark 1 — Revision Round Reduction")
    print(f"   Baseline : {rv['avg_baseline']} rounds")
    print(f"   MCP      : {rv['avg_mcp']} rounds")
    print(f"   Delta    : {rv['avg_delta']:+.1f}  ← {rv['headline']}")

    print(f"\n💰 Benchmark 2 — Token Cost (honest accounting)")
    print(f"   Baseline gen     : {tc['avg_baseline_tokens']} tokens")
    print(f"   MCP total        : {tc['avg_mcp_total_tokens']} tokens (incl. {tc['avg_context_overhead']} ctx overhead)")
    print(f"   Upfront delta    : {tc['avg_upfront_delta']:+.0f} tokens")
    print(f"   Est. downstream  : ~{tc['est_downstream_savings']:.0f} tokens saved from fewer revisions")

    print(f"\n✅ Benchmark 3 — Pattern Adherence (opensre standards)")
    print(f"   Baseline : {pa['avg_baseline']}/10")
    print(f"   MCP      : {pa['avg_mcp']}/10")
    print(f"   Delta    : {pa['avg_delta']:+.1f}  ← {pa['headline']}")

    print(f"\nResults : {csv_path}")
    print(f"Summary : {json_path}")
    print("=" * 62)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Eval harness — PR Context MCP vs baseline")
    parser.add_argument("--repo",    type=str, help="Filter to a specific repo slug")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = parser.parse_args()
    asyncio.run(run_benchmark(args))