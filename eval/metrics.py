"""Retrieval metrics for the offline evaluation harness (pure Python, no I/O).

Conventions (all documented in eval/README.md):

* A ranked list is a list of thread ids, best first, already de-duplicated.
  A condition that returns nothing above its threshold has ABSTAINED and its
  ranked list is empty.
* ``recall@k``    = |top-k ∩ relevant| / |relevant|.
* ``precision@k`` = |top-k ∩ relevant| / k (fixed denominator, so its ceiling
  is below 1 whenever fewer than k threads are relevant; the oracle row shows
  that ceiling).
* ``reciprocal rank`` = 1 / rank of the first relevant thread in the whole
  ranked list, 0 when none appears.
* Unanswerable questions have no relevant threads.  They are scored ONLY by
  correct abstention and are never included in recall, precision or MRR.
"""

from __future__ import annotations

ANSWERABLE_CATEGORIES = ("exact_identifier", "rationale", "conflicting")
UNANSWERABLE = "unanswerable"
CATEGORIES = ANSWERABLE_CATEGORIES + (UNANSWERABLE,)


def recall_at_k(ranked: list[str], relevant: list[str] | set[str], k: int) -> float:
    rel = set(relevant)
    if not rel:
        raise ValueError("recall is undefined without relevant threads (unanswerable question)")
    return len(set(ranked[:k]) & rel) / len(rel)


def precision_at_k(ranked: list[str], relevant: list[str] | set[str], k: int) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    return len(set(ranked[:k]) & set(relevant)) / k


def reciprocal_rank(ranked: list[str], relevant: list[str] | set[str]) -> float:
    rel = set(relevant)
    for position, thread_id in enumerate(ranked, start=1):
        if thread_id in rel:
            return 1.0 / position
    return 0.0


def score_question(category: str, ranked: list[str], relevant: list[str], k: int) -> dict:
    """Per-question row.  ``abstained`` is simply "the condition returned nothing"."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    abstained = len(ranked) == 0
    if category == UNANSWERABLE:
        if relevant:
            raise ValueError("an unanswerable question must have no relevant_thread_ids")
        return {
            "abstained": abstained, "correct_abstention": abstained,
            "recall": None, "precision": None, "rr": None, "hit": None,
        }
    if not relevant:
        raise ValueError(f"{category} question needs at least one relevant_thread_id")
    return {
        "abstained": abstained, "correct_abstention": None,
        "recall": recall_at_k(ranked, relevant, k),
        "precision": precision_at_k(ranked, relevant, k),
        "rr": reciprocal_rank(ranked, relevant),
        "hit": bool(set(ranked[:k]) & set(relevant)),
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """Aggregate scored rows (each carries ``category`` plus score_question keys).

    Returns ``{"answerable": {...}, "unanswerable": {...}}`` for the given rows.
    ``None`` means "no questions of that kind", never zero.
    """
    answerable = [r for r in rows if r["category"] != UNANSWERABLE]
    unanswerable = [r for r in rows if r["category"] == UNANSWERABLE]
    return {
        "answerable": {
            "n": len(answerable),
            "recall": _mean([r["recall"] for r in answerable]),
            "precision": _mean([r["precision"] for r in answerable]),
            "mrr": _mean([r["rr"] for r in answerable]),
            "hit_rate": _mean([1.0 if r["hit"] else 0.0 for r in answerable]),
            "false_abstention_rate": _mean([1.0 if r["abstained"] else 0.0 for r in answerable]),
        },
        "unanswerable": {
            "n": len(unanswerable),
            "correct_abstention_rate": _mean([1.0 if r["correct_abstention"] else 0.0 for r in unanswerable]),
        },
    }


def summarize_by_category(rows: list[dict]) -> dict:
    """``summarize`` overall plus one entry per category present in ``rows``."""
    out = {"overall": summarize(rows), "by_category": {}}
    for category in CATEGORIES:
        subset = [r for r in rows if r["category"] == category]
        if subset:
            out["by_category"][category] = summarize(subset)
    return out


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def markdown_tables(conditions: dict[str, dict], k: int) -> str:
    """Render ``{condition: summarize_by_category(...)}`` as markdown tables."""
    lines: list[str] = []
    names = list(conditions)

    lines += [f"### Overall (answerable questions, k={k})", "",
              f"| condition | n | Recall@{k} | Precision@{k} | MRR | false abstention |",
              "|---|---|---|---|---|---|"]
    for name in names:
        a = conditions[name]["overall"]["answerable"]
        lines.append(
            f"| {name} | {a['n']} | {_fmt(a['recall'])} | {_fmt(a['precision'])} | "
            f"{_fmt(a['mrr'])} | {_fmt(a['false_abstention_rate'])} |"
        )

    lines += ["", "### Unanswerable questions (abstention only; never averaged into recall)", "",
              "| condition | n | correct abstention |", "|---|---|---|"]
    for name in names:
        u = conditions[name]["overall"]["unanswerable"]
        lines.append(f"| {name} | {u['n']} | {_fmt(u['correct_abstention_rate'])} |")

    for category in ANSWERABLE_CATEGORIES:
        if not any(category in conditions[n]["by_category"] for n in names):
            continue
        lines += ["", f"### {category}", "",
                  f"| condition | n | Recall@{k} | Precision@{k} | MRR | false abstention |",
                  "|---|---|---|---|---|---|"]
        for name in names:
            entry = conditions[name]["by_category"].get(category)
            if entry is None:
                continue
            a = entry["answerable"]
            lines.append(
                f"| {name} | {a['n']} | {_fmt(a['recall'])} | {_fmt(a['precision'])} | "
                f"{_fmt(a['mrr'])} | {_fmt(a['false_abstention_rate'])} |"
            )
    return "\n".join(lines) + "\n"
