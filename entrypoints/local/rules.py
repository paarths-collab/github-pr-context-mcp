"""Deterministic rule engine for lessons: lifecycle, score, scope and serving (stdlib only).

Division of labour: the host model judges meaning (what an observation says, which lesson it
matches), a fresh checker pass verifies that judgement, and this module enforces the facts:
it counts distinct PRs, assigns the lifecycle level, computes the score and decides what is
served. Everything here is a pure function of the stored evidence and a caller-supplied clock,
so the same state always yields the same levels and scores (also while backfilling history).
"""

from datetime import datetime
import json

KIND_RANK = {"change_request_fixed": 1, "follow_up_fix": 2, "revert": 3}
OBSERVATION_KINDS = ("revert", "follow_up_fix", "change_request_fixed", "review_comment")
BOT_OK_KINDS = ("revert", "follow_up_fix")

ARCHIVE_AFTER_DAYS = 365
ACTIVE_MIN_PRS = 2
TEAM_MIN_REVIEWERS = 2
GLOBAL_MIN_TOP_DIRS = 3
SCOPE_MAX_DEPTH = 3
PREFERENCE_FACTOR = 0.5
SCORE_WEIGHTS = {"revert": 3.0, "follow_up_fix": 2.0, "change_request_fixed": 1.0,
                 "reviewer": 0.5, "repeat": 2.0}

SERVE_TOKEN_BUDGET = 600
SERVE_MAX_LIMIT = 20
SERVE_WHY_CHARS = 160
EXPORT_MAX_LESSONS = 30

_STATUS_ORDER = {"hardened": 0, "active": 1, "candidate": 2, "archived": 3}

# Mirrors storage.document_builder._login_is_bot. Not imported: the default runtime must not
# pull in the indexed storage package (tests/test_live_workflow.py enforces that).
_KNOWN_BOT_LOGINS = {"github-actions", "dependabot", "vercel"}


def is_bot(login):
    normalized = (login or "").lower()
    return bool(normalized) and ("[bot]" in normalized or normalized.endswith("-bot")
                                 or normalized in _KNOWN_BOT_LOGINS)


# -- paths and scope ---------------------------------------------------------
def normalise_path(path):
    """Repo-relative, forward-slash path without empty or '.' parts; None if unusable."""
    if not isinstance(path, str):
        return None
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts or len(path) > 300:
        return None
    return "/".join(parts)


def directory_of(path, max_depth=None):
    parts = path.split("/")[:-1]
    return "/".join(parts if max_depth is None else parts[:max_depth])


def lesson_scope(paths):
    """Evidence paths -> directories (max depth 3); global once 3 top-level dirs are involved."""
    dirs = sorted({directory_of(path, SCOPE_MAX_DEPTH) for path in paths})
    top = {d.split("/")[0] for d in dirs if d}
    return {"dirs": dirs, "global": len(top) >= GLOBAL_MIN_TOP_DIRS, "unscoped": not paths}


def scope_matches(scope, path_dirs):
    """Global (or unscoped, i.e. nothing known to narrow it) lessons always apply; else prefix match."""
    if scope["global"] or scope["unscoped"]:
        return True
    return any(directory == d or (d and directory.startswith(d + "/"))
               for d in scope["dirs"] for directory in path_dirs)


# -- lifecycle and score -----------------------------------------------------
def _day(value):
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def clock_date(created_at):
    """The repo clock: creation date of the newest processed PR (or None)."""
    return _day(created_at) if created_at else None


def all_evidence(lesson):
    return lesson["evidence"] + lesson["repeats"]


def analyze(lesson, clock):
    """Derive status, team/preference, score and scope from approved evidence only."""
    items = all_evidence(lesson)
    strongest = {}
    for item in items:
        strongest[item["pr"]] = max(strongest.get(item["pr"], 0), KIND_RANK[item["kind"]])
    count = {rank: sum(1 for value in strongest.values() if value == rank) for rank in (1, 2, 3)}
    reviewers = sorted({item["reviewer"].lower() for item in items if item["reviewer"]})
    repeats = len({item["pr"] for item in lesson["repeats"]})
    newest = max((item["date"] for item in items), default=None)
    reference = clock or (_day(newest) if newest else None)
    age_days = max(0, (reference - _day(newest)).days) if newest and reference else 0

    if lesson["removed"]:
        status = "removed"
    elif lesson["archived_by_conflict"] or age_days > ARCHIVE_AFTER_DAYS:
        status = "archived"
    elif len(strongest) >= ACTIVE_MIN_PRS or count[3] or lesson["manual_active"]:
        status = "hardened" if repeats else "active"
    else:
        status = "candidate"

    kind = "team" if len(reviewers) >= TEAM_MIN_REVIEWERS else "preference"
    weights = SCORE_WEIGHTS
    raw = (weights["revert"] * count[3] + weights["follow_up_fix"] * count[2]
           + weights["change_request_fixed"] * count[1] + weights["reviewer"] * len(reviewers)
           + weights["repeat"] * repeats)
    score = raw * 0.5 ** (age_days / ARCHIVE_AFTER_DAYS) * (1.0 if kind == "team" else PREFERENCE_FACTOR)
    return {"status": status, "kind": kind, "score": round(score, 2), "repeat_count": repeats,
            "scope": lesson_scope([p for item in items for p in item["paths"]]),
            "reviewers": reviewers, "newest_evidence": newest, "age_days": age_days}


def sort_key(lesson, info):
    return (_STATUS_ORDER.get(info["status"], 9), -info["score"], int(lesson["id"][1:]))


def best_link(lesson):
    """URL of the strongest evidence: revert > follow-up fix > change request, newest first."""
    best = max(all_evidence(lesson), key=lambda e: (KIND_RANK[e["kind"]], e["date"], e["pr"]), default=None)
    return best["url"] if best else ""


def latest_example(lesson):
    for item in sorted(all_evidence(lesson), key=lambda e: (e["date"], e["pr"]), reverse=True):
        if item.get("example"):
            return item["example"]
    return None


# -- serving -----------------------------------------------------------------
def compact(lesson, info):
    """Small form served to the agent; the example is added separately if the budget allows."""
    return {"id": lesson["id"], "rule": lesson["rule"], "why": lesson["why"][:SERVE_WHY_CHARS],
            "status": info["status"], "kind": info["kind"], "score": info["score"],
            "link": best_link(lesson)}


def _size(entry):
    return len(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))


def serve(entries, limit, budget_tokens=SERVE_TOKEN_BUDGET):
    """Take entries (compact dict, example or None) in rank order within limit and token budget.

    Rank order is kept: the first entry that does not fit stops the list. Examples are added
    afterwards, best-ranked first, only while they still fit. Tokens are estimated as chars/4.
    """
    budget = budget_tokens * 4
    chosen, used = [], 0
    for entry, example in entries[:limit]:
        if used + _size(entry) > budget:
            break
        chosen.append([dict(entry), example])
        used += _size(entry)
    for pair in chosen:
        if pair[1]:
            extra = _size({**pair[0], "example": pair[1]}) - _size(pair[0])
            if used + extra <= budget:
                pair[0]["example"] = pair[1]
                used += extra
    return [pair[0] for pair in chosen], used // 4
