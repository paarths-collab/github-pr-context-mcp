"""Deterministic rule engine for lessons: lifecycle, score, scope and serving (stdlib only).

Division of labour: the host model judges meaning (what an observation says, which lesson it
matches), a fresh checker pass verifies that judgement, and this module enforces the facts:
it counts distinct PRs, assigns the lifecycle level, computes the score and decides what is
served. Everything here is a pure function of the stored evidence and a caller-supplied clock,
so the same state always yields the same levels and scores (also while backfilling history).
"""

from datetime import datetime
import json
import re

KIND_RANK = {"change_request_fixed": 1, "follow_up_fix": 2, "revert": 3, "ci_failure_fixed": 2}
# CI failures are a second feedback source. Only ci_failure_fixed is evidence (as strong as a follow-up
# fix); ci_flaky and a plain ci_failure are discarded and only counted, so they have no rank.
CI_KINDS = ("ci_failure_fixed", "ci_flaky", "ci_failure")
OBSERVATION_KINDS = ("revert", "follow_up_fix", "change_request_fixed", "review_comment") + CI_KINDS
BOT_OK_KINDS = ("revert", "follow_up_fix")
CI_SOURCE = "ci"
CI_FIELDS = ("source", "workflow", "job", "failure_signature", "run_url")
MAX_SIGNATURE = 200

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

# Mistake families: a small fixed taxonomy so "the same kind of mistake" can be recognised across
# folders (a missing timeout on an HTTP call and on a DB call are both "missing-timeout"). The model
# labels a family, the checker verifies it, code only stores and counts. Order is display order.
OTHER_FAMILY = "other"
FAMILIES = {
    "missing-timeout": "outbound calls (HTTP, database, RPC, subprocess) made without a timeout",
    "blocking-io": "blocking or synchronous work on a hot path, an event loop or a UI thread",
    "resource-leak": "files, connections, locks, threads or memory that are not released or closed",
    "error-handling": "errors swallowed, wrong exceptions, missing retries, fallbacks or cleanup",
    "concurrency": "races, missing locks, shared mutable state or non-atomic updates",
    "input-validation": "untrusted or malformed input that is not validated, bounded or sanitised",
    "security": "secrets, authentication, authorisation, injection or other security weaknesses",
    "api-misuse": "a library, framework or internal API used against its contract",
    "performance": "needless work such as N+1 queries, repeated calls or unbounded loops and buffers",
    "data-integrity": "data loss, corruption, inconsistent state or unsafe migrations",
    "naming-and-style": "naming, formatting, structure and readability conventions",
    "tests": "missing, weak, flaky or misplaced tests",
    "docs-and-changelog": "missing or outdated documentation, comments or changelog entries",
    "config-and-deps": "configuration, environment, build or dependency problems",
    OTHER_FAMILY: "anything that fits none of the families above",
}
SERVE_MAX_FAMILIES = len(FAMILIES)


def normalise_family(value):
    """Family name from model or checker text; anything unknown (or not a string) becomes 'other'."""
    if isinstance(value, str):
        key = "-".join(value.lower().replace("_", " ").split())
        if key in FAMILIES:
            return key
    return OTHER_FAMILY


def list_families():
    return [{"name": name, "description": description} for name, description in FAMILIES.items()]


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
    elif lesson.get("merged_into"):
        status = "merged"
    elif lesson["archived_by_conflict"] or age_days > ARCHIVE_AFTER_DAYS:
        status = "archived"
    elif len(strongest) >= ACTIVE_MIN_PRS or count[3] or lesson["manual_active"]:
        status = "hardened" if repeats else "active"
    else:
        status = "candidate"

    # A failing CI job is not one person's taste: CI evidence in 2 distinct PRs also makes a team rule.
    ci_prs = len({item["pr"] for item in items if item.get("source") == CI_SOURCE})
    kind = "team" if len(reviewers) >= TEAM_MIN_REVIEWERS or ci_prs >= ACTIVE_MIN_PRS else "preference"
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


# -- CI failures --------------------------------------------------------------
_SIG_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_SIG_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b")
_SIG_HASH_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b")
_SIG_RUNNER_RE = re.compile(r"(?:/home/runner/work|/github/workspace|[a-z]:[\\/]a)[\\/][^\\/\s]+[\\/][^\\/\s]+[\\/]?")
_SIG_TMP_RE = re.compile(r"(?:/private)?/(?:tmp|var/folders|var/tmp)/\S*|[a-z]:[\\/]\S*[\\/]temp[\\/]\S*")
_SIG_DIGITS_RE = re.compile(r"\d+")


def normalise_failure_signature(value):
    """Short, stable identifier of a CI failure, so the same failure matches across runs and PRs.

    Lower-cased, whitespace collapsed; runner checkout prefixes dropped; temp paths, UUIDs, hashes and hex
    addresses replaced by placeholders; every number (line numbers, durations, counts) becomes N. Returns ''
    for non-strings and for text with no letters left.
    """
    if not isinstance(value, str):
        return ""
    text = " ".join(value.lower().split())
    text = _SIG_RUNNER_RE.sub("", text)
    text = _SIG_TMP_RE.sub("<tmp>", text)
    text = _SIG_UUID_RE.sub("<id>", text)
    text = _SIG_HEX_RE.sub("<hex>", text)
    text = _SIG_HASH_RE.sub("<hash>", text)
    text = _SIG_DIGITS_RE.sub("N", text)
    text = text[:MAX_SIGNATURE].strip()
    return text if re.search(r"[a-z]", re.sub(r"<[a-z]+>", "", text)) else ""


def ci_fixed_count(items):
    """Approved CI evidence items across (lesson, info) pairs of live lessons."""
    return sum(1 for lesson, _ in items for e in all_evidence(lesson) if e.get("source") == CI_SOURCE)


# -- families ----------------------------------------------------------------
def family_of(lesson):
    return lesson.get("family") or OTHER_FAMILY


def principle_of(lesson):
    return lesson.get("principle") or lesson["rule"]


def family_stats(items):
    """Per family: active/hardened lessons, hardened lessons, approved repeats, newest repeat date.

    items are (lesson, info) pairs of live lessons. Families with nothing to report are left out;
    the order is the taxonomy order.
    """
    table = {name: {"family": name, "description": FAMILIES[name], "lessons": 0, "hardened": 0,
                    "repeats": 0, "last_repeat": None} for name in FAMILIES}
    for lesson, info in items:
        row = table[family_of(lesson) if family_of(lesson) in FAMILIES else OTHER_FAMILY]
        if info["status"] in ("active", "hardened"):
            row["lessons"] += 1
        if info["status"] == "hardened":
            row["hardened"] += 1
        row["repeats"] += info["repeat_count"]
        dates = [item["date"] for item in lesson["repeats"]]
        if dates and (row["last_repeat"] is None or max(dates) > row["last_repeat"]):
            row["last_repeat"] = max(dates)
    return [row for row in table.values() if row["lessons"] or row["repeats"]]


# -- serving -----------------------------------------------------------------
def match_basis(scope, path_dirs, family, families):
    """Why a lesson is served: 'global' (applies everywhere), 'path' (folder match), 'family' or None."""
    if scope["global"] or scope["unscoped"]:
        return "global"
    if scope_matches(scope, path_dirs):
        return "path"
    return "family" if family in families else None


def serve_key(lesson, info, basis):
    """Hardened first, then folder (or global) matches before family-only matches, then score."""
    return (info["status"] != "hardened", basis == "family", -info["score"], int(lesson["id"][1:]))


def compact(lesson, info, matched_by=None):
    """Small form served to the agent: the principle first; the specific rule and example are added
    separately, only if the budget allows."""
    entry = {"id": lesson["id"], "principle": principle_of(lesson), "family": family_of(lesson),
             "why": lesson["why"][:SERVE_WHY_CHARS], "status": info["status"], "kind": info["kind"],
             "score": info["score"], "link": best_link(lesson)}
    if matched_by:
        entry["matched_by"] = matched_by
    return entry


def extras_of(lesson):
    """Optional fields in priority order: the specific rule (unless it is the principle), then the example."""
    extras = {}
    if lesson["rule"] != principle_of(lesson):
        extras["rule"] = lesson["rule"]
    example = latest_example(lesson)
    if example:
        extras["example"] = example
    return extras


def _size(entry):
    return len(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))


def serve(entries, limit, budget_tokens=SERVE_TOKEN_BUDGET):
    """Take entries (compact dict, extras dict) in rank order within limit and token budget.

    Rank order is kept: the first entry that does not fit stops the list. Extras (the specific rule,
    then the example) are added afterwards, best-ranked first, only while they still fit, so the
    principle wins when the budget is tight. Tokens are estimated as chars/4.
    """
    budget = budget_tokens * 4
    chosen, used = [], 0
    for entry, extras in entries[:limit]:
        if used + _size(entry) > budget:
            break
        chosen.append([dict(entry), extras])
        used += _size(entry)
    for field in ("rule", "example"):
        for pair in chosen:
            if pair[1].get(field):
                extra = _size({**pair[0], field: pair[1][field]}) - _size(pair[0])
                if used + extra <= budget:
                    pair[0][field] = pair[1][field]
                    used += extra
    return [pair[0] for pair in chosen], used // 4
