# Converts raw PR dicts -> evidence units ready for embedding + storage.
# No model, no ChromaDB, no GitHub calls here.
#
# One evidence unit per review thread, PR description, review summary and commit
# message.  Units longer than MAX_UNIT_CHARS are split into overlapping parts.
# Every unit carries provenance metadata (str/int/float/bool only, never None).
#
# Repository text is UNTRUSTED DATA.  Nothing here interprets it; it is only
# copied verbatim into the excerpt.

import hashlib
import json
import re
from datetime import datetime, timezone

MAX_UNIT_CHARS = 6000
PART_OVERLAP_CHARS = 400
_HASH_LEN = 12

_KNOWN_BOT_LOGINS = {"github-actions", "dependabot", "vercel"}

_HEADER_RE = re.compile(r"\A\[PR #\d+ \| [^\n]*\]\n\n")
_NOTE_RE = re.compile(
    r"\n\[Extraction note: GitHub paginated only part of this PR's [^\n]*connection\(s\)\.\]\Z"
)


def is_high_quality_comment(body: str, is_bot: bool) -> bool:
    """Filter out low-signal PR comments to improve embedding quality."""
    if is_bot:
        return False

    body_lower = body.strip().lower()

    # Too short to be a substantive review comment
    if len(body_lower) < 15:
        return False

    # A "nit:" prefix flags a deliberately minor remark, so it has to carry more
    # substance than the general minimum before it earns a place in the index.
    if body_lower.startswith("nit:") and len(body_lower) < 25:
        return False

    # Common low-signal phrases
    low_signal_phrases = [
        "lgtm", "looks good", "looks good to me", "+1", "nit:", "agreed",
        "done", "fixed", "thanks", "bump", "ping", "addressed"
    ]

    # If the comment is just a short variation of a low-signal phrase
    if any(body_lower == p or (body_lower.startswith(p) and len(body_lower) < len(p) + 10) for p in low_signal_phrases):
        return False

    return True


# ── small coercion helpers (metadata must never contain None) ─────────────────

def _s(value: object) -> str:
    return value if isinstance(value, str) else ""


def _i(value: object, default: int = -1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _login_is_bot(login: str) -> bool:
    if not login:
        return False
    normalized = login.lower()
    return (
        "[bot]" in normalized
        or normalized.endswith("-bot")
        or normalized in _KNOWN_BOT_LOGINS
    )


def _actor(item: dict, login_key: str = "author") -> tuple[str, str, bool]:
    """Return (login, author_type, is_bot) tolerating records from older fetches."""
    login = _s(item.get(login_key))
    if "is_bot" in item and item["is_bot"] is not None:
        is_bot = bool(item["is_bot"])
    elif login:
        is_bot = _login_is_bot(login)
    else:
        is_bot = False

    author_type = _s(item.get("author_type"))
    if author_type not in {"human", "bot", "unknown"}:
        if is_bot:
            author_type = "bot"
        elif not login or login == "ghost":
            author_type = "unknown"
        else:
            author_type = "human"
    if author_type == "bot":
        is_bot = True
    return login, author_type, is_bot


def _review_is_bot(review: dict) -> bool:
    """Return a reviewer's bot flag without relying on an inline comment."""
    return _actor(review)[2]


def _extraction_metadata(pr: dict) -> dict:
    """Preserve whether GitHub returned a complete nested PR record."""
    truncated = pr.get("truncated_connections") or []
    if not isinstance(truncated, list):
        truncated = []

    return {
        "source_truncated": bool(truncated),
        "truncated_connections": json.dumps(truncated),
    }


def _extraction_note(pr: dict) -> str:
    truncated = pr.get("truncated_connections") or []
    if not isinstance(truncated, list) or not truncated:
        return ""
    return (
        "\n[Extraction note: GitHub paginated only part of this PR's "
        f"{', '.join(str(value) for value in truncated)} connection(s).]"
    )


def excerpt_from_document(text: str) -> str:
    """Return the verbatim excerpt of a stored document.

    Strips the one-line ``[PR #n | ...]`` header and the trailing extraction
    note that build_documents adds.  Legacy documents (no header) are returned
    unchanged apart from the trailing note.
    """
    if not isinstance(text, str):
        return ""
    stripped = _HEADER_RE.sub("", text, count=1)
    return _NOTE_RE.sub("", stripped, count=1)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


def body_sha256(text: str) -> str:
    """Full sha256 hex digest of a verbatim excerpt (the archived body hash)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_time(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or offset); naive values are UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _latest_timestamp(values: list[str]) -> str:
    """The original string of the latest parseable timestamp, or "" if none."""
    best: tuple[datetime, str] | None = None
    for value in values:
        parsed = _parse_time(value)
        if parsed is not None and (best is None or parsed > best[0]):
            best = (parsed, value)
    return best[1] if best else ""


def split_excerpt(text: str, size: int = MAX_UNIT_CHARS, overlap: int = PART_OVERLAP_CHARS) -> list[str]:
    """Split text into parts of at most `size` chars sharing `overlap` chars."""
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    pos = 0
    while True:
        end = min(pos + size, len(text))
        if end < len(text):
            newline = text.rfind("\n", pos + size // 2, end)
            if newline > pos:
                end = newline + 1
        parts.append(text[pos:end])
        if end >= len(text):
            break
        pos = max(end - overlap, pos + 1)
    return parts


def _one_line(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


# ── thread normalisation ──────────────────────────────────────────────────────

def _normalise_comment(comment: dict) -> dict:
    login, author_type, is_bot = _actor(comment)
    return {
        "id": _s(comment.get("id")) or _s(comment.get("github_node_id")),
        "url": _s(comment.get("url")),
        "author": login,
        "author_type": author_type,
        "is_bot": is_bot,
        "body": _s(comment.get("body")),
        "created_at": _s(comment.get("created_at")),
        "updated_at": _s(comment.get("updated_at")) or _s(comment.get("created_at")),
        "diff_hunk": _s(comment.get("diff_hunk")),
    }


def _threads_from_record(pr: dict) -> list[dict]:
    """Return normalised threads, falling back to legacy flat review comments."""
    pr_num = pr["number"]
    truncated = pr.get("truncated_connections")
    truncated = truncated if isinstance(truncated, list) else []
    threads: list[dict] = []

    raw_threads = pr.get("threads")
    if isinstance(raw_threads, list):
        for index, raw in enumerate(raw_threads):
            if not isinstance(raw, dict):
                continue
            comments = [
                _normalise_comment(c) for c in (raw.get("comments") or []) if isinstance(c, dict)
            ]
            first_id = comments[0]["id"] if comments else ""
            thread_id = _s(raw.get("thread_id")) or first_id or f"pr-{pr_num}-thread-{index}"
            threads.append({
                "key": thread_id,
                "url": _s(raw.get("url")) or (comments[0]["url"] if comments else ""),
                "path": _s(raw.get("path")),
                "line": _i(raw.get("line"), _i(raw.get("original_line"))),
                "original_commit": _s(raw.get("original_commit")),
                "resolved": bool(raw.get("is_resolved", False)),
                "outdated": bool(raw.get("is_outdated", False)),
                "incomplete": bool(raw.get("thread_incomplete", False)),
                "comments": comments,
            })
        return threads

    # Legacy: flat comments, grouped by file in first-seen order.
    groups: dict[str, list[dict]] = {}
    for comment in pr.get("review_comments") or []:
        if isinstance(comment, dict):
            groups.setdefault(_s(comment.get("file")), []).append(comment)
    legacy_incomplete = "reviewThreads.comments" in truncated
    for path, raw_comments in groups.items():
        comments = [_normalise_comment(c) for c in raw_comments]
        first_id = comments[0]["id"]
        # Legacy `hunk` lives on each comment already; line from the first comment.
        line = -1
        for raw in raw_comments:
            line = _i(raw.get("line"))
            if line != -1:
                break
        threads.append({
            "key": first_id or f"pr-{pr_num}-file-{path or 'unknown'}",
            "url": "",
            "path": path,
            "line": line,
            "original_commit": "",
            "resolved": any(bool(c.get("resolved", False)) for c in raw_comments),
            "outdated": False,
            "incomplete": legacy_incomplete,
            "comments": comments,
        })
    return threads


def _thread_excerpt(thread: dict) -> str:
    comments = [c for c in thread["comments"] if c["body"].strip()]
    hunk = next((c["diff_hunk"] for c in thread["comments"] if c["diff_hunk"]), "")
    location = f"File: {thread['path']}" if thread["path"] else "File: (unknown)"
    if thread["line"] != -1:
        location += f" | Line: {thread['line']}"
    lines = [location]
    if hunk:
        lines.append(f"Code Context:\n{hunk}")
    body_blocks = [f"Reviewer ({c['author'] or 'unknown'}): {c['body']}" for c in comments]
    return "\n".join(lines) + "\n\n" + "\n\n".join(body_blocks)


# ── unit assembly ─────────────────────────────────────────────────────────────

def build_documents(prs: list[dict]) -> tuple[list, list, list]:
    """
    Convert a list of PR dicts into (documents, metadatas, ids)
    ready to be encoded and upserted into ChromaDB.
    """
    docs: list[str] = []
    metadatas: list[dict] = []
    ids: list[str] = []
    # Evidence ids must not depend on batch composition or order: the same PR
    # built alone, or together with others, yields identical ids.  Keys are
    # PR-scoped where the source id is not globally unique (commit oids repeat
    # across PRs), and the only de-duplication is WITHIN one PR record
    # (``seen_ids`` is reset per PR).  If a batch carries the same PR number
    # twice, the later record wins (it is the fresher fetch).
    latest_by_number = {pr["number"]: index for index, pr in enumerate(prs)}
    prs = [pr for index, pr in enumerate(prs) if latest_by_number[pr["number"]] == index]
    seen_ids: set[str] = set()

    def emit(pr_base: dict, pr: dict, kind: str, source_kind: str, key: str,
             excerpt: str, unit_meta: dict, label: str = "") -> None:
        parts = split_excerpt(excerpt)
        count = len(parts)
        thread_evidence_id = f"{key}:{_digest(excerpt)}" if count > 1 else ""
        note = _extraction_note(pr)
        for index, part in enumerate(parts):
            evidence_id = f"{key}:{_digest(part)}"
            if evidence_id in seen_ids:
                evidence_id = f"{key}:{_digest(part + chr(0) + str(index))}"
            seen_ids.add(evidence_id)

            part_label = f" | part {index + 1}/{count}" if count > 1 else ""
            label_text = f" | {_one_line(label)}" if label else ""
            header = (
                f"[PR #{pr['number']} | {kind}{part_label}{label_text} | "
                f"{_one_line(_s(pr.get('title')))}]"
            )
            docs.append(f"{header}\n\n{part}{note}")
            meta = {
                **pr_base,
                **unit_meta,
                "type": kind,
                "source_kind": source_kind,
                "evidence_id": evidence_id,
                "thread_evidence_id": thread_evidence_id,
                "part_index": index,
                "part_count": count,
                "source_key": key,
                # Archived hash of the verbatim excerpt of THIS unit: lets a later
                # run detect that the underlying text was edited after indexing.
                "body_sha256": body_sha256(part),
            }
            metadatas.append(_clean_metadata(meta))
            ids.append(evidence_id)

    for pr in prs:
        seen_ids.clear()
        pr_num = pr["number"]
        pr_scope = _s(pr.get("github_node_id")) or f"pr{pr_num}"
        pr_url = _s(pr.get("url"))
        pr_title = _s(pr.get("title"))
        touches_ci = bool(pr.get("touches_ci", False))
        pr_updated = _s(pr.get("updated_at"))
        pr_base = {
            "pr_number": pr_num,
            "pr_url": pr_url,
            "pr_title": pr_title,
            "pr_state": _s(pr.get("state")),
            "pr_created_at": _s(pr.get("created_at")),
            "pr_merged_at": _s(pr.get("merged_at")),
            "pr_updated_at": pr_updated,
            "observed_at": _s(pr.get("observed_at")),
            "touches_ci": touches_ci,
            **_extraction_metadata(pr),
        }
        blank_thread_fields = {
            "original_commit": "",
            "original_path": "",
            "line": -1,
            "thread_incomplete": False,
            "comment_count": 0,
            "resolved": False,
            "outdated": False,
        }

        # PR description
        body = _s(pr.get("body"))
        if body.strip():
            author, author_type, is_bot = _actor({
                "author": pr.get("author"),
                "is_bot": pr.get("is_bot", pr.get("author_is_bot")),
                "author_type": pr.get("author_type"),
            })
            emit(pr_base, pr, "pr_description", "pr_description",
                 _s(pr.get("github_node_id")) or f"pr-{pr_num}-desc", body, {
                     **blank_thread_fields,
                     "source_url": pr_url,
                     "author": author,
                     "author_type": author_type,
                     "is_bot": is_bot,
                     "created_at": _s(pr.get("created_at")),
                     "updated_at": pr_updated,
                     "files": json.dumps([
                         _s(f.get("path")) for f in (pr.get("files") or []) if isinstance(f, dict)
                     ]),
                 })

        # Review threads (conversation + code context, bots kept and tagged)
        for thread in _threads_from_record(pr):
            comments = thread["comments"]
            if not comments:
                continue
            # A thread is noise only when every comment is trivial.
            if not any(is_high_quality_comment(c["body"], False) for c in comments):
                continue
            excerpt = _thread_excerpt(thread)
            all_bot = all(c["is_bot"] for c in comments)
            first = comments[0]
            last = comments[-1]
            emit(pr_base, pr, "review_thread",
                 "bot_review_thread" if all_bot else "human_review_thread",
                 thread["key"], excerpt, {
                     "source_url": thread["url"],
                     "author": first["author"],
                     "author_type": "bot" if all_bot else (
                         first["author_type"] if first["author_type"] != "bot" else "human"
                     ),
                     "is_bot": all_bot,
                     "created_at": first["created_at"],
                     "updated_at": last["updated_at"] or last["created_at"],
                     "original_commit": thread["original_commit"],
                     "original_path": thread["path"],
                     "file": thread["path"],
                     "line": thread["line"],
                     "thread_incomplete": thread["incomplete"],
                     "comment_count": len(comments),
                     "resolved": thread["resolved"],
                     "outdated": thread["outdated"],
                     # Max updated_at over EVERY comment (an edit to an early
                     # comment does not move the last comment's timestamp).
                     "latest_comment_updated_at": _latest_timestamp(
                         [c["updated_at"] or c["created_at"] for c in comments]
                     ),
                 })

        # Commit messages
        for i, commit in enumerate(pr.get("commits") or []):
            if not isinstance(commit, dict):
                continue
            message = _s(commit.get("message"))
            if not message.strip():
                continue
            oid = _s(commit.get("oid"))
            login = _s(commit.get("author"))
            commit_is_bot = _login_is_bot(login)
            if commit_is_bot:
                commit_author_type = "bot"
            elif login:
                commit_author_type = _s(commit.get("author_type")) or "human"
            else:
                commit_author_type = "unknown"
            commit_url = _s(commit.get("url")) or (
                f"{pr_url}/commits/{oid}" if pr_url and oid else ""
            )
            commit_date = (
                _s(commit.get("committed_date")) or _s(commit.get("committed_at"))
                or _s(commit.get("created_at"))
            )
            emit(pr_base, pr, "commit_message", "commit_message",
                 f"{pr_scope}:commit:{oid or f'pr-{pr_num}-commit-{i}'}", message, {
                     **blank_thread_fields,
                     "source_url": commit_url,
                     "author": login,
                     "author_type": commit_author_type,
                     "is_bot": commit_is_bot,
                     "created_at": commit_date,
                     "updated_at": commit_date,
                 })

        # Overall review summaries (human and bot, tagged)
        for i, review in enumerate(pr.get("reviews") or []):
            if not isinstance(review, dict):
                continue
            review_body = _s(review.get("body"))
            # Bot review summaries are deliberately indexed (tagged via is_bot /
            # source_kind), so the bot rejection in is_high_quality_comment is
            # bypassed; only the length/low-signal checks apply.
            if not is_high_quality_comment(review_body, False):
                continue
            author, author_type, is_bot = _actor(review)
            submitted = _s(review.get("submitted_at"))
            node_id = _s(review.get("id")) or _s(review.get("github_node_id"))
            emit(pr_base, pr, "review_summary",
                 "bot_review_summary" if is_bot else "human_review_summary",
                 node_id or f"pr-{pr_num}-review-{i}",
                 review_body, {
                     **blank_thread_fields,
                     "source_url": _s(review.get("url")),
                     "author": author,
                     "author_type": author_type,
                     "is_bot": is_bot,
                     "created_at": submitted,
                     "updated_at": _s(review.get("updated_at")) or submitted,
                     "review_state": _s(review.get("state")),
                 }, label=f"review by {author or 'unknown'} [{_s(review.get('state'))}]")

    return docs, metadatas, ids


def _clean_metadata(meta: dict) -> dict:
    """Guarantee Chroma-safe values: str/int/float/bool only, never None."""
    cleaned: dict = {}
    for key, value in meta.items():
        if value is None:
            cleaned[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            cleaned[key] = value
        else:
            cleaned[key] = json.dumps(value, default=str)
    return cleaned


# ── historical-leakage guard ──────────────────────────────────────────────────

def _field(item: dict, name: str) -> str:
    """Read a string field from a unit (top level) or a hit (``metadata``)."""
    value = item.get(name) if isinstance(item, dict) else None
    if not isinstance(value, str) or not value:
        meta = item.get("metadata") if isinstance(item, dict) else None
        value = meta.get(name) if isinstance(meta, dict) else None
    return value if isinstance(value, str) else ""


def _item_id(item: dict) -> str:
    return _field(item, "evidence_id") or (item.get("id") if isinstance(item, dict) else "") or ""


def latest_known_update(item: dict) -> str:
    """Latest known modification time of a unit/hit, "" when nothing is known.

    The max of ``latest_comment_updated_at`` (threads), ``updated_at`` and
    ``created_at``.  PR-level ``pr_updated_at`` is deliberately ignored: it moves
    on any activity and would discard far more history than necessary.
    """
    return _latest_timestamp([
        _field(item, "latest_comment_updated_at"),
        _field(item, "updated_at"),
        _field(item, "created_at"),
    ])


def as_of_filter(
    units_or_hits: list[dict],
    cutoff_iso: str,
    *,
    created_only: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Keep only evidence that could have existed, unchanged, at ``cutoff_iso``.

    Accepts envelope units (top-level ``created_at`` ...) or raw hits
    (``{"id", "metadata": {...}}``).  Returns ``(kept, dropped)``; order is
    preserved and each ``dropped`` row is ``{"evidence_id", "reason", "item"}``.

    Conservative (default): a unit is kept only when ``created_at <= cutoff``
    AND its latest known update (see :func:`latest_known_update`) ``<= cutoff``.
    A body edited after the cutoff may no longer match what was visible at the
    cutoff, so it is dropped even though the comment itself is old.  A unit whose
    ``created_at`` is missing/unparsable is dropped (its age cannot be shown).

    ``created_only=True`` reproduces the naive filter (``created_at`` only) so
    the leakage it admits can be measured against the conservative result.
    """
    cutoff = _parse_time(cutoff_iso)
    if cutoff is None:
        raise ValueError(f"cutoff_iso is not an ISO-8601 timestamp: {cutoff_iso!r}")
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in units_or_hits:
        created = _parse_time(_field(item, "created_at"))
        reason = ""
        if created is None:
            reason = "created_at missing or unparsable"
        elif created > cutoff:
            reason = "created after cutoff"
        elif not created_only:
            latest = _parse_time(latest_known_update(item))
            if latest is not None and latest > cutoff:
                reason = "updated after cutoff"
        if reason:
            dropped.append({"evidence_id": _item_id(item), "reason": reason, "item": item})
        else:
            kept.append(item)
    return kept, dropped
