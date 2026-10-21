"""Offline retrieval evaluation harness.

    python eval/run_eval.py                       # bundled smoke sample, fake encoder
    python eval/run_eval.py --questions Q.jsonl --corpus C.json --encoder minilm \
        --out results.json --markdown results.md

No network is used.  ``--encoder fake`` needs no model download; ``--encoder
minilm`` loads the product's real SentenceTransformer from the local cache.
Retrieval only: nothing here calls an LLM.  See eval/README.md for the question
and corpus formats, the abstention rule and the labelling protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_QUESTIONS = os.path.join(HERE, "sample", "questions.jsonl")
SAMPLE_CORPUS = os.path.join(HERE, "sample", "corpus.json")
HARNESS_VERSION = 1
DEFAULT_K = 5
CONDITION_ORDER = ("keyword", "semantic", "hybrid", "oracle")

from eval import metrics  # noqa: E402  (after sys.path setup)


# ── loading and validation ────────────────────────────────────────────────────

class HarnessError(ValueError):
    """Invalid input files (bad labels, unknown thread ids, ...)."""


def _sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_corpus(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        corpus = json.load(fh)
    repos = corpus.get("repos") if isinstance(corpus, dict) else None
    if not isinstance(repos, dict) or not repos:
        raise HarnessError('corpus must be an object with a non-empty "repos" mapping of repo -> PR records')
    for repo, prs in repos.items():
        if not isinstance(prs, list) or not all(isinstance(p, dict) and "number" in p for p in prs):
            raise HarnessError(f"repo {repo!r}: expected a list of PR records that each have a number")
    return corpus


def load_questions(path: str) -> list[dict]:
    questions: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HarnessError(f"{path}:{line_no}: invalid JSON ({exc})") from exc
            if not isinstance(row, dict):
                raise HarnessError(f"{path}:{line_no}: each line must be a JSON object")
            questions.append(row)
    return questions


def validate(questions: list[dict], corpus: dict, unit_threads: dict[str, set[str]]) -> list[dict]:
    """Normalise questions (default repo) and raise HarnessError listing every problem."""
    repos = list(corpus["repos"])
    problems: list[str] = []
    seen: set[str] = set()
    normalised: list[dict] = []
    for row in questions:
        qid = str(row.get("id", "")).strip()
        label = f"question {qid or '<missing id>'}"
        if not qid:
            problems.append("a question has no id")
        elif qid in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(qid)
        category = row.get("category")
        if category not in metrics.CATEGORIES:
            problems.append(f"{label}: category must be one of {', '.join(metrics.CATEGORIES)}")
        if not isinstance(row.get("query"), str) or not row["query"].strip():
            problems.append(f"{label}: query must be a non-empty string")
        relevant = row.get("relevant_thread_ids", [])
        if not isinstance(relevant, list) or not all(isinstance(r, str) for r in relevant):
            problems.append(f"{label}: relevant_thread_ids must be a list of strings")
            relevant = []
        repo = row.get("repo") or (repos[0] if len(repos) == 1 else None)
        if repo not in corpus["repos"]:
            problems.append(f"{label}: repo {repo!r} is not in the corpus (set \"repo\" to one of {repos})")
        elif category == metrics.UNANSWERABLE and relevant:
            problems.append(f"{label}: unanswerable questions must have an empty relevant_thread_ids")
        elif category in metrics.ANSWERABLE_CATEGORIES and not relevant:
            problems.append(f"{label}: {category} questions need at least one relevant thread id")
        else:
            missing = [r for r in relevant if r not in unit_threads.get(repo, set())]
            if missing:
                problems.append(f"{label}: relevant ids not found in {repo}: {missing}")
        query_time = row.get("query_time")
        if query_time is not None:
            from storage.document_builder import _parse_time
            if _parse_time(query_time) is None:
                problems.append(f"{label}: query_time must be an ISO-8601 timestamp")
        normalised.append({**row, "id": qid, "repo": repo, "relevant_thread_ids": relevant})
    if problems:
        raise HarnessError("invalid input:\n  - " + "\n  - ".join(problems))
    return normalised


# ── ranking pipeline shared by every condition ────────────────────────────────

def rank_threads(hits: list[dict], query_time: str | None, limit: int) -> tuple[list[str], list[dict]]:
    """as-of filter -> keep supported hits -> lift to threads (first hit wins).

    Returns ``(ranked_thread_ids, dropped_by_cutoff)``.  An empty ranked list is
    an abstention.
    """
    from eval.conditions import thread_id_of
    from storage.document_builder import as_of_filter

    dropped: list[dict] = []
    if query_time:
        hits, dropped = as_of_filter(hits, query_time)
    ranked: list[str] = []
    for hit in hits:
        if not hit["supported"]:
            continue
        thread_id = thread_id_of(hit["metadata"], hit["id"])
        if thread_id not in ranked:
            ranked.append(thread_id)
        if len(ranked) >= limit:
            break
    return ranked, dropped


def eligibility(units: list[dict], query_time: str | None) -> dict | None:
    """What a query-time cutoff leaves of the corpus (recorded per question)."""
    if not query_time:
        return None
    from storage.document_builder import as_of_filter
    kept, dropped = as_of_filter([{"id": u["id"], "metadata": u["metadata"]} for u in units], query_time)
    naive_kept, _ = as_of_filter(
        [{"id": u["id"], "metadata": u["metadata"]} for u in units], query_time, created_only=True
    )
    reasons = Counter(d["reason"] for d in dropped)
    return {
        "query_time": query_time,
        "total_units": len(units),
        "eligible_units": len(kept),
        "eligible_units_created_only": len(naive_kept),
        "excluded_by_reason": dict(reasons),
    }


# ── the run ───────────────────────────────────────────────────────────────────

def parse_thresholds(encoder: str, overrides: list[str]) -> dict[str, float]:
    from eval.conditions import DEFAULT_THRESHOLDS
    thresholds = dict(DEFAULT_THRESHOLDS[encoder])
    for item in overrides or []:
        name, sep, value = item.partition("=")
        if not sep or name not in thresholds:
            raise HarnessError(f"--threshold expects keyword=<float> or semantic=<float>, got {item!r}")
        try:
            thresholds[name] = float(value)
        except ValueError as exc:
            raise HarnessError(f"--threshold {item!r}: not a number") from exc
    return thresholds


def evaluate(
    questions: list[dict],
    corpus: dict,
    *,
    encoder: str = "fake",
    conditions: tuple[str, ...] = CONDITION_ORDER,
    k: int = DEFAULT_K,
    thresholds: dict[str, float] | None = None,
) -> dict:
    """Run the chosen conditions and return the full result document (no file I/O)."""
    from eval import conditions as cond
    from contextlib import nullcontext

    unknown = [c for c in conditions if c not in CONDITION_ORDER]
    if unknown:
        raise HarnessError(f"unknown condition(s) {unknown}; choose from {list(CONDITION_ORDER)}")
    thresholds = thresholds or dict(cond.DEFAULT_THRESHOLDS[encoder])
    prs_by_repo = corpus["repos"]
    units_by_repo = cond.build_units(prs_by_repo)
    unit_threads = {
        repo: {cond.thread_id_of(u["metadata"]) for u in units} for repo, units in units_by_repo.items()
    }
    questions = validate(questions, corpus, unit_threads)

    needs_index = any(c in {"semantic", "hybrid"} for c in conditions)
    context = cond.open_index(prs_by_repo, encoder) if needs_index else nullcontext(None)
    runners: dict[str, object] = {}
    with context as index:
        for name in conditions:
            if name == "keyword":
                runners[name] = cond.KeywordCondition(units_by_repo, thresholds["keyword"])
            elif name == "oracle":
                runners[name] = cond.OracleCondition(units_by_repo)
            else:
                runners[name] = cond.IndexedCondition(name, index, units_by_repo, thresholds["semantic"])

        results: dict[str, dict] = {}
        for name, runner in runners.items():
            rows: list[dict] = []
            for q in questions:
                hits = runner.search(q["repo"], q["query"], relevant=q["relevant_thread_ids"])
                ranked, dropped = rank_threads(hits, q.get("query_time"), cond.RETRIEVE_THREADS)
                scored = metrics.score_question(q["category"], ranked, q["relevant_thread_ids"], k)
                rows.append({
                    "id": q["id"], "category": q["category"], "repo": q["repo"],
                    "relevant_thread_ids": q["relevant_thread_ids"], "returned_thread_ids": ranked,
                    "dropped_by_cutoff": len(dropped), **scored,
                })
            results[name] = {"questions": rows, **metrics.summarize_by_category(rows)}
            if name == "hybrid":
                results[name]["retrieval_modes"] = sorted(runner.retrieval_modes)
                results[name]["warnings"] = runner.warnings

    categories = Counter(q["category"] for q in questions)
    return {
        "harness_version": HARNESS_VERSION,
        "k": k,
        "encoder": encoder,
        "thresholds": thresholds,
        "conditions": list(conditions),
        "question_counts": {c: categories.get(c, 0) for c in metrics.CATEGORIES} | {"total": len(questions)},
        "eligibility": {
            q["id"]: e for q in questions
            if (e := eligibility(units_by_repo[q["repo"]], q.get("query_time"))) is not None
        },
        "results": results,
    }


def render_markdown(document: dict, corpus_label: str) -> str:
    k = document["k"]
    counts = document["question_counts"]
    header = [
        "# Retrieval evaluation",
        "",
        f"> {corpus_label}" if corpus_label else "> (corpus has no label)",
        "",
        f"- encoder: `{document['encoder']}`; k = {k}; thresholds: "
        + ", ".join(f"{n}={v}" for n, v in document["thresholds"].items()),
        "- questions: " + ", ".join(f"{c}={counts[c]}" for c in metrics.CATEGORIES) + f" (total {counts['total']})",
        "- unanswerable questions are scored by abstention only and never enter recall, precision or MRR",
        "",
    ]
    tables = metrics.markdown_tables(
        {name: {"overall": r["overall"], "by_category": r["by_category"]} for name, r in document["results"].items()}, k
    )
    return "\n".join(header) + tables


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline retrieval evaluation harness (retrieval only, no LLM).")
    parser.add_argument("--questions", default=SAMPLE_QUESTIONS, help="JSONL labelled questions")
    parser.add_argument("--corpus", default=SAMPLE_CORPUS, help="corpus JSON (see eval/README.md)")
    parser.add_argument("--encoder", choices=("fake", "minilm"), default="fake")
    parser.add_argument("--conditions", default=",".join(CONDITION_ORDER),
                        help="comma list of: " + ", ".join(CONDITION_ORDER))
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--threshold", action="append", default=[], metavar="NAME=VALUE",
                        help="override an abstention threshold (keyword=, semantic=); repeatable")
    parser.add_argument("--out", help="write the JSON result here (default: stdout summary only)")
    parser.add_argument("--markdown", help="write the markdown tables here (always printed to stdout too)")
    return parser


def _isolate_default_storage() -> None:
    """Point the product's storage at a temp dir before anything imports it.

    Importing ``storage`` opens the default Chroma directory and cursor database
    under the home directory.  A standalone run must never touch those.
    """
    if "storage" in sys.modules:
        return
    scratch = tempfile.mkdtemp(prefix="pr-context-eval-import-")
    os.environ["CHROMA_PERSIST_DIR"] = os.path.join(scratch, "chroma")
    os.environ["CURSOR_DB_PATH"] = os.path.join(scratch, "cursors.db")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.k < 1:
        print("error: --k must be >= 1", file=sys.stderr)
        return 2
    _isolate_default_storage()
    conditions = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    try:
        corpus = load_corpus(args.corpus)
        questions = load_questions(args.questions)
        thresholds = parse_thresholds(args.encoder, args.threshold)
        document = evaluate(
            questions, corpus, encoder=args.encoder, conditions=conditions, k=args.k, thresholds=thresholds,
        )
    except (HarnessError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    document["inputs"] = {
        "questions": {"path": args.questions, "sha256": _sha256_file(args.questions)},
        "corpus": {"path": args.corpus, "sha256": _sha256_file(args.corpus), "label": corpus.get("label", ""),
                   "repos": {r: len(p) for r, p in corpus["repos"].items()}},
    }
    markdown = render_markdown(document, corpus.get("label", ""))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2)
            fh.write("\n")
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(markdown)
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
