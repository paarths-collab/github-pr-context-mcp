"""Five acceptance cases, end to end at the library / MCP-tool level, fully offline.

Everything between "GitHub answers a GraphQL query" and "an MCP tool returns JSON" is
real: ``fetcher.client.fetch_prs`` (nested pagination included) -> ``flatten_pr`` ->
``storage.document_builder.build_documents`` -> ``storage.vector_store`` (tmp Chroma +
tmp SQLite registry, generations, ledger, epochs) -> ``app.index_status`` /
``app.retrieval`` -> the real ``app.tools.indexing`` and ``app.tools.analysis`` tools
(invoked through FakeMCP-style registries).

Only the edges are faked: ``fetcher.client.run_query`` talks to :class:`FakeGitHub`
(an in-memory, cursor-paginated GraphQL server) and the embedding model is replaced by
a deterministic bag-of-words hashing encoder.  No network, no model files.

1. A thread beyond the initial window becomes reachable through resumed backfill after
   restarts, and neither backfill nor refresh strands older data.
2. A thread that exceeds the nested comment page cap stays honestly flagged
   (``thread_incomplete`` / ``nested_incomplete_units``) across persistence and restart.
3. Bot and human discussion of the same behaviour both survive; filters change candidates.
4. A later thread that appears to reverse a constraint is annotated, never "superseded".
5. Revoked / failed authorization, failed refresh and missing embedding files give
   actionable states while the previous generation keeps serving; delete-while-running
   cannot resurrect an index.
"""

import asyncio
import hashlib
import importlib
import json
import math
import re

import pytest

pytest.importorskip("chromadb", reason="ChromaDB is required for the acceptance tests")

import chromadb  # noqa: E402

import fetcher.client as client  # noqa: E402
import storage  # noqa: E402
import storage.vector_store as vs  # noqa: E402
from app import retrieval  # noqa: E402
from app.tools import analysis, indexing  # noqa: E402
from auth import GitHubAuthorizationRequired  # noqa: E402
from fetcher.client import RateLimitError  # noqa: E402
from storage import encoder as real_encoder  # noqa: E402
from storage.cursor_store import CursorStore  # noqa: E402

cursor_module = importlib.import_module("storage.cursor_store")

REPO = "acme/widget"
OWNER, NAME = REPO.split("/")
SEMANTIC_TOOL = "semantic_search_reviews"


# ── deterministic fake encoder (bag of words, hashed, L2-normalised) ──────────

_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")
_DIM = 256


def _vector(text):
    vec = [0.0] * _DIM
    for token in _TOKEN_RE.findall(text.lower()):
        bucket = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:4], "big") % _DIM
        vec[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── in-memory GraphQL server --------------------------------------------------

def _conn(nodes, *, has_next=False, end=None, total=None):
    conn = {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": end}}
    if total is not None:
        conn["totalCount"] = total
    return conn


def user(login="alice"):
    return {"__typename": "User", "login": login}


def bot(login="github-actions[bot]"):
    return {"__typename": "Bot", "login": login}


def comment(cid, body, *, author=None, created="2024-03-01T10:00:00Z", hunk=""):
    return {
        "id": cid,
        "databaseId": int.from_bytes(hashlib.sha256(cid.encode()).digest()[:3], "big"),
        "url": f"https://github.com/{REPO}/pull/1#discussion_{cid}",
        "author": author or user(),
        "body": body,
        "createdAt": created,
        "updatedAt": created,
        "diffHunk": hunk,
        "originalCommit": {"oid": "c0ffee1"},
    }


class FakeGitHub:
    """Serves the PR list query (either sort order) and the nested follow-up queries.

    Cursors are ``after:<pr number>`` so they stay valid when newer PRs appear, like
    GitHub's opaque cursors.  ``page_size`` / ``comment_page`` are deliberately tiny.
    """

    def __init__(self, page_size=2, comment_page=3):
        self.page_size = page_size
        self.comment_page = comment_page
        self.prs: dict[int, dict] = {}
        self.threads: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.error: BaseException | None = None
        self.gate: asyncio.Event | None = None

    # -- building ------------------------------------------------------------
    def add_thread(self, tid, path, comments, line=12):
        self.threads[tid] = {"id": tid, "path": path, "comments": comments, "line": line}
        return tid

    def add_pr(self, number, *, title=None, body="", threads=(), reviews=(), commits=(),
               files=(), created=None, updated=None, state="MERGED", author=None):
        created = created or f"2024-03-{number:02d}T10:00:00Z"
        self.prs[number] = {
            "number": number, "title": title or f"Change {number}", "body": body,
            "threads": list(threads), "reviews": list(reviews), "commits": list(commits),
            "files": list(files), "created": created, "updated": updated or created,
            "state": state, "author": author or user("dev"),
        }

    def touch(self, number, updated, **changes):
        self.prs[number]["updated"] = updated
        self.prs[number].update(changes)

    def block(self) -> asyncio.Event:
        self.gate = asyncio.Event()
        return self.gate

    # -- rendering -----------------------------------------------------------
    def _thread_node(self, tid):
        t = self.threads[tid]
        comments = t["comments"]
        first = comments[: self.comment_page]
        more = len(comments) > len(first)
        return {
            "id": tid, "isResolved": False, "isOutdated": False, "path": t["path"],
            "line": t["line"], "originalLine": t["line"],
            "comments": _conn(first, has_next=more, end=f"i:{len(first)}" if more else None,
                              total=len(comments)),
        }

    def _pr_node(self, spec):
        n = spec["number"]
        threads = [self._thread_node(t) for t in spec["threads"]]
        return {
            "id": f"PR_{n}", "number": n, "url": f"https://github.com/{REPO}/pull/{n}",
            "title": spec["title"], "body": spec["body"], "author": spec["author"],
            "createdAt": spec["created"], "updatedAt": spec["updated"],
            "mergedAt": spec["created"], "state": spec["state"], "additions": 1, "deletions": 1,
            "files": _conn([{"path": p, "additions": 1, "deletions": 0, "changeType": "MODIFIED"}
                            for p in spec["files"]]),
            "reviewThreads": _conn(threads, total=len(threads)),
            "commits": _conn([
                {"commit": {"oid": oid, "url": f"https://github.com/{REPO}/commit/{oid}",
                            "message": msg, "author": {"name": "Dev", "user": {"login": "dev"}}}}
                for oid, msg in spec["commits"]
            ]),
            "reviews": _conn([
                {"id": f"RV_{n}_{i}", "url": f"https://github.com/{REPO}/pull/{n}#pullrequestreview-{i}",
                 "author": r["author"], "state": "COMMENTED", "body": r["body"],
                 "submittedAt": spec["created"], "updatedAt": spec["created"]}
                for i, r in enumerate(spec["reviews"])
            ]),
        }

    # -- the run_query replacement ---------------------------------------------
    async def run_query(self, query, variables, github_token=None):
        if "GetThreadComments" in query:
            self.calls.append({"kind": "thread_comments", "id": variables["id"]})
            comments = self.threads[variables["id"]]["comments"]
            start = int(variables["cursor"].split(":")[1])
            chunk = comments[start:start + self.comment_page]
            more = start + len(chunk) < len(comments)
            return {"data": {"node": {"comments": _conn(
                chunk, has_next=more, end=f"i:{start + len(chunk)}", total=len(comments))}}}
        if "GetPRReviewThreads" in query:
            raise AssertionError("no scenario overflows reviewThreads")

        order = re.search(r"field: (UPDATED_AT|CREATED_AT)", query).group(1)
        self.calls.append({"kind": "list", "order": order, "cursor": variables.get("cursor")})
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        key = "created" if order == "CREATED_AT" else "updated"
        ordered = sorted(self.prs.values(), key=lambda s: (s[key], s["number"]), reverse=True)
        start = 0
        cursor = variables.get("cursor")
        if cursor:
            after = int(cursor.split(":")[1])
            start = next(i for i, s in enumerate(ordered) if s["number"] == after) + 1
        chunk = ordered[start:start + self.page_size]
        more = start + len(chunk) < len(ordered)
        end = f"after:{chunk[-1]['number']}" if chunk else None
        return {"data": {"repository": {"pullRequests": {
            "nodes": [self._pr_node(s) for s in chunk],
            "pageInfo": {"hasNextPage": more, "endCursor": end}}}}}

    def list_calls(self):
        return [c for c in self.calls if c["kind"] == "list"]


# ── wiring ----------------------------------------------------------------------

class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def register(func):
            self.tools[name] = func
            return func

        return register


class Env:
    """Real tools + real storage in tmp dirs, fed by a FakeGitHub and a fake encoder."""

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        self.chroma_path = str(tmp_path / "chroma")
        self.db_path = str(tmp_path / "cursors.db")
        self.gh = FakeGitHub()
        self.token_error: Exception | None = None
        self.state = {"active_repo": None, "active_namespace": None, "storage_types": {}}
        self.mcp = FakeMCP()
        self.restart()

        monkeypatch.setattr(vs, "encode", _vector)
        monkeypatch.setattr(vs, "encode_batch", lambda texts: [_vector(t) for t in texts])
        monkeypatch.setattr(retrieval, "encode", _vector)
        monkeypatch.setattr(client, "run_query", self.gh.run_query)

        for module in (indexing, analysis):
            monkeypatch.setattr(module, "get_state", lambda ctx: self.state)
            monkeypatch.setattr(module, "resolve_namespace", lambda namespace, state: namespace)
            monkeypatch.setattr(module, "track_usage", lambda *args: None)
            monkeypatch.setattr(module, "is_temporary", lambda *args: False)
        monkeypatch.setattr(indexing, "resolve_repo", lambda repo, state, file_path=None: repo or REPO)
        monkeypatch.setattr(analysis, "resolve_repo", lambda repo, state, file_path=None: repo or REPO)
        monkeypatch.setattr(indexing, "get_github_access_token", self._token)
        indexing._INDEX_JOBS.clear()
        indexing._INDEX_TASKS.clear()
        indexing.register_indexing_tools(self.mcp)
        analysis.register_analysis_tools(self.mcp)

    async def _token(self):
        if self.token_error is not None:
            raise self.token_error
        return "test-token"

    def restart(self):
        """Simulate a process restart: reopen the registry and Chroma, drop in-process jobs."""
        store = CursorStore(self.db_path)
        self.monkeypatch.setattr(cursor_module, "cursor_store", store)
        self.monkeypatch.setattr(storage, "cursor_store", store)
        self.monkeypatch.setattr(vs, "_persistent_client", chromadb.PersistentClient(path=self.chroma_path))
        indexing._INDEX_JOBS.clear()
        indexing._INDEX_TASKS.clear()
        self.store = store

    # -- tool helpers ----------------------------------------------------------
    async def ensure(self, wait=True, **kwargs):
        kwargs.setdefault("repo", REPO)
        response = await self.mcp.tools["ensure_repo_ready"](ctx=object(), **kwargs)
        if wait:
            await self.drain()
        return response

    async def drain(self):
        tasks = list(indexing._INDEX_TASKS.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stats(self):
        return json.loads(await self.mcp.tools["get_index_stats"](ctx=object(), repo=REPO))

    async def tool(self, name, **kwargs):
        return json.loads(await self.mcp.tools[name](ctx=object(), **kwargs))

    async def search(self, query, tool=SEMANTIC_TOOL, **kwargs):
        arg = {"semantic_search_reviews": "query", "find_similar_errors": "error_message",
               "get_team_review_patterns": "topic"}.get(tool, "code")
        return await self.tool(tool, **{arg: query}, **kwargs)

    def registry(self):
        return vs.get_index_registry(REPO, None, "permanent")

    def collections(self):
        return vs._collection_names(vs._persistent_client)


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path)


def run(coro):
    return asyncio.run(coro)


def pr_numbers(envelope):
    return {u["pr_number"] for u in envelope["results"]}


def evidence_ids(envelope):
    return [u["evidence_id"] for u in envelope["results"]]


def all_text(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def filler_thread(gh, number, topic):
    return gh.add_thread(
        f"T{number}", f"src/module_{number}.py",
        [comment(f"C{number}", f"Reviewer note on {topic} for module {number}, please keep the helper small.",
                 created=f"2024-03-{number:02d}T11:00:00Z")],
    )


# ═════════════════════════════════════════════════════════════════════════════
# Case 1: a thread beyond the initial window is reachable through resumed backfill
# ═════════════════════════════════════════════════════════════════════════════

FENCING_QUERY = "fencing token rotation before the lease renews"


def _history_repo(gh):
    for n in range(1, 9):
        threads = [filler_thread(gh, n, f"unrelated topic {n}")]
        if n == 3:
            threads.append(gh.add_thread(
                "T3-fencing", "src/lease.py",
                [comment("C3-fencing",
                         "zzfencing: the fencing token rotation must happen before the lease renews, "
                         "otherwise two writers can hold the lease at once.",
                         author=user("maintainer"), created="2024-03-03T12:00:00Z")],
            ))
        gh.add_pr(n, threads=threads, files=[f"src/module_{n}.py"])


def test_case1_backfill_after_restarts_reaches_older_thread_without_stranding_data(env):
    gh = env.gh
    _history_repo(gh)

    async def scenario():
        # -- initial bounded window: newest-created PRs 8 and 7 only ------------
        await env.ensure(storage="permanent", pages=1)
        first = await env.stats()
        watermark = env.store.get_refresh_state(REPO)["last_updated_at"]
        assert watermark and watermark.startswith("2024-03-")
        before = await env.search(FENCING_QUERY)
        return first, watermark, before

    first, watermark, before = run(scenario())

    assert first["index_state"] == "ready_selected_scope"
    assert first["coverage"]["pull_requests_indexed"] == 2
    assert (first["coverage"]["pr_number_min"], first["coverage"]["pr_number_max"]) == (7, 8)
    assert first["coverage"]["older_history_remaining"] is True
    assert first["coverage"]["history_complete"] is False
    assert gh.list_calls()[0] == {"kind": "list", "order": "CREATED_AT", "cursor": None}
    # The known relevant thread (PR 3) is NOT reachable yet, and the answer says why.
    assert 3 not in pr_numbers(before)
    assert before["results"] and any("Older pull request history is not indexed" in w for w in before["warnings"])
    assert env.registry()["backfill_cursor"] == "after:7"

    # PR 4 is updated long after the refresh watermark; backfill must not move the watermark.
    gh.touch(4, "2030-01-01T00:00:00Z")

    # -- restart #1, backfill one page (PRs 6, 5): still partial ------------------
    env.restart()

    async def backfill_one():
        message = await env.ensure(backfill=True, pages=1)
        assert "Backfill of older history started" in message
        return await env.stats(), await env.search(FENCING_QUERY)

    second, mid = run(backfill_one())
    assert second["index_job"]["kind"] == "backfill" and second["index_state"] == "ready_partial"
    assert "backfill=true" in second["action"]
    assert second["coverage"]["pull_requests_indexed"] == 4
    assert 3 not in pr_numbers(mid)
    assert env.store.get_refresh_state(REPO)["last_updated_at"] == watermark
    assert env.registry()["backfill_cursor"] == "after:5"

    # -- restart #2, backfill the next page (PRs 4, 3) ----------------------------
    env.restart()

    async def backfill_two():
        await env.ensure(backfill=True, pages=1)
        return await env.stats(), await env.search(FENCING_QUERY)

    third, found = run(backfill_two())
    list_calls = gh.list_calls()
    assert [(c["order"], c["cursor"]) for c in list_calls] == [
        ("CREATED_AT", None), ("CREATED_AT", "after:7"), ("CREATED_AT", "after:5"),
    ]
    assert 3 in pr_numbers(found)
    hit = next(u for u in found["results"] if u["pr_number"] == 3 and "zzfencing" in u["excerpt"])
    assert hit["source_kind"] == "human_review_thread" and hit["original_path"] == "src/lease.py"
    assert third["coverage"]["pull_requests_indexed"] == 6
    assert (third["coverage"]["pr_number_min"], third["coverage"]["pr_number_max"]) == (3, 8)
    # Backfill must not advance the refresh watermark, even though PR 4 was updated in 2030.
    assert env.store.get_refresh_state(REPO)["last_updated_at"] == watermark
    assert env.registry()["backfill_cursor"] == "after:3"
    assert env.registry()["history_complete"] is False

    # -- restart #3, a refresh: it sees PR 4 (updated) and a brand new PR 9 --------
    gh.add_pr(9, threads=[filler_thread(gh, 9, "brand new work")], files=["src/module_9.py"],
              created="2031-03-09T10:00:00Z")
    env.restart()

    async def refresh():
        await env.ensure(refresh=True, pages=3)
        return await env.stats(), await env.search(FENCING_QUERY)

    refreshed, after_refresh = run(refresh())
    refresh_calls = [c for c in gh.list_calls()[3:]]
    assert refresh_calls and all(c["order"] == "UPDATED_AT" for c in refresh_calls)
    # The older PR survived the refresh generation, the new PR arrived, backfill state is untouched.
    assert 3 in pr_numbers(after_refresh)
    assert refreshed["coverage"]["pr_number_max"] == 9 and refreshed["coverage"]["pr_number_min"] == 3
    assert refreshed["coverage"]["pull_requests_indexed"] == 7
    assert refreshed["generation"] == 4
    assert env.store.get_refresh_state(REPO)["last_updated_at"] > watermark  # refresh moves ITS watermark
    assert env.registry()["backfill_cursor"] == "after:3"
    assert env.registry()["history_complete"] is False
    assert refreshed["coverage"]["older_history_remaining"] is True

    # -- restart #4, finish the backfill from the persisted cursor ----------------
    env.restart()

    async def finish():
        await env.ensure(backfill=True, pages=3)
        return await env.stats(), await env.search(FENCING_QUERY)

    done, final = run(finish())
    assert (gh.list_calls()[-1]["order"], gh.list_calls()[-1]["cursor"]) == ("CREATED_AT", "after:3")
    assert done["index_state"] == "ready_selected_scope"
    assert done["coverage"]["selection"] == "full-closed-history"
    assert done["coverage"]["history_complete"] is True and done["coverage"]["older_history_remaining"] is False
    assert (done["coverage"]["pr_number_min"], done["coverage"]["pr_number_max"]) == (1, 9)
    assert done["coverage"]["pull_requests_indexed"] == 9
    assert 3 in pr_numbers(final)
    assert not any("Older pull request history is not indexed" in w for w in final["warnings"])


# ═════════════════════════════════════════════════════════════════════════════
# Case 2: a thread exceeding the nested comment page cap stays honestly incomplete
# ═════════════════════════════════════════════════════════════════════════════

def _long_thread_repo(gh):
    total = 60
    comments = [
        comment(f"L{i}", f"Comment {i:03d}: " + "zzmigration rollback ordering needs care " * 7,
                created=f"2024-03-01T10:{i % 60:02d}:00Z", hunk="@@ -1,3 +1,3 @@\n-old\n+new" if i == 1 else "")
        for i in range(1, total + 1)
    ]
    long_id = gh.add_thread("T-long", "db/migrate.py", comments)
    short_id = gh.add_thread("T-short", "docs/guide.md", [
        comment("S1", "Please document the changelog format in the contributing guide.", author=user("bob")),
        comment("S2", "Agreed, I will add a section about changelog entries.", author=user("alice")),
    ])
    gh.add_pr(1, body="Reworks migration ordering.", threads=[long_id, short_id], files=["db/migrate.py"])
    return total


def test_case2_capped_thread_is_flagged_and_stays_honest_after_restart(env):
    gh = env.gh
    total = _long_thread_repo(gh)
    fetched_expected = gh.comment_page * (1 + client.MAX_EXTRA_THREAD_COMMENT_PAGES)
    assert fetched_expected < total  # the cap really bites

    # Library level: fetch_prs + flatten expose the truncation on the record itself.
    (record,) = asyncio.run(client.fetch_prs(OWNER, NAME, pages=1, github_token="t", order_by="CREATED_AT"))
    long_thread = next(t for t in record["threads"] if t["thread_id"] == "T-long")
    assert long_thread["thread_incomplete"] is True
    assert long_thread["comments_fetched"] == fetched_expected and long_thread["comments_total"] == total
    assert [c["id"] for c in long_thread["comments"]] == [f"L{i}" for i in range(1, fetched_expected + 1)]
    assert record["nested_incomplete_threads"] == 1
    assert "reviewThreads.comments" in record["truncated_connections"]
    short_thread = next(t for t in record["threads"] if t["thread_id"] == "T-short")
    assert short_thread["thread_incomplete"] is False

    async def index_and_search():
        await env.ensure(storage="permanent", pages=3)
        stats = await env.stats()
        # Units carry the legacy text/metadata/similarity copies, so a 6000-char part
        # costs more than the default budget; ask for a larger one explicitly.
        search = await env.search("zzmigration rollback ordering", budget_tokens=16000)
        return stats, search

    stats, search = run(index_and_search())
    assert stats["coverage"]["nested_incomplete_units"] == 1
    assert "only partially fetched" in stats["coverage"]["summary"]

    unit = next(u for u in search["results"] if u["original_path"] == "db/migrate.py")
    assert unit["thread_incomplete"] is True
    assert unit["part_count"] >= 2  # long enough to be split, so "all fetched parts" matters
    assert any("partially fetched" in w for w in search["warnings"])
    assert search["coverage"]["nested_incomplete_units"] == 1

    async def expand():
        return await env.tool("get_evidence", evidence_id=unit["evidence_id"])

    expanded = run(expand())
    parts = expanded["results"]
    assert [p["part_index"] for p in parts] == list(range(unit["part_count"]))
    assert all(p["thread_incomplete"] is True for p in parts)
    joined = "\n".join(p["excerpt"] for p in parts)
    for i in range(1, fetched_expected + 1):
        assert f"Comment {i:03d}:" in joined, f"fetched comment {i} missing from the returned parts"
    assert f"Comment {fetched_expected + 1:03d}:" not in joined  # never invents unfetched replies
    assert any("partially fetched" in w for w in expanded["warnings"])

    short = next((u for u in search["results"] if u["original_path"] == "docs/guide.md"), None)
    if short is not None:
        assert short["thread_incomplete"] is False

    # -- restart: counts and flags come from persisted state, still honest -------
    env.restart()

    async def after_restart():
        return await env.stats(), await env.search("zzmigration rollback ordering", budget_tokens=16000)

    stats2, search2 = run(after_restart())
    assert stats2["coverage"]["nested_incomplete_units"] == 1
    assert stats2["total_documents"] == stats["total_documents"]
    unit2 = next(u for u in search2["results"] if u["original_path"] == "db/migrate.py")
    assert unit2["thread_incomplete"] is True and unit2["evidence_id"] == unit["evidence_id"]
    assert any("partially fetched" in w for w in search2["warnings"])

    # -- a refresh that re-fetches the (still capped) PR must not double count or hide it
    gh.touch(1, "2024-04-01T00:00:00Z")

    async def refresh():
        await env.ensure(refresh=True, pages=1)
        return await env.stats()

    stats3 = run(refresh())
    assert stats3["generation"] == stats["generation"] + 1
    assert stats3["coverage"]["nested_incomplete_units"] == 1
    assert stats3["coverage"]["pull_requests_indexed"] == 1

    # -- and one more restart on top of the refreshed generation ------------------
    env.restart()
    assert run(env.stats())["coverage"]["nested_incomplete_units"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# Case 3: bot and human discuss the same behaviour
# ═════════════════════════════════════════════════════════════════════════════

RETRY_QUERY = "retry exponential backoff jitter delay cap"


def _bot_and_human_repo(gh):
    human_threads = [
        gh.add_thread("H1", "src/retry.py", [
            comment("H1a", "We should retry with exponential backoff and jitter, and cap the delay at 30 seconds.",
                    author=user("alice")),
            comment("H1b", "Agreed, the delay cap matters because unbounded backoff stalls the worker.",
                    author=user("bob")),
        ]),
        gh.add_thread("H2", "src/retry.py", [
            comment("H2a", "Please make the retry policy configurable instead of hard coding the delay.",
                    author=user("alice")),
        ]),
    ]
    gh.add_pr(1, title="Add retry policy", threads=human_threads, files=["src/retry.py"],
              reviews=[{"author": user("alice"), "body": "Overall the retry approach looks right to me, thanks."}])
    bot_threads = []
    for i in range(5):
        login = bot("github-actions[bot]") if i % 2 == 0 else bot("coderabbitai")  # second has no [bot] suffix
        bot_threads.append(gh.add_thread(f"B{i}", "src/retry.py", [
            comment(f"B{i}a", f"Automated review: retry with exponential backoff jitter and a delay cap, "
                              f"suggestion {i} retry exponential backoff jitter delay cap.", author=login),
        ]))
    gh.add_pr(2, title="Bot findings", threads=bot_threads, files=["src/retry.py"],
              reviews=[{"author": bot("coderabbitai"), "body": "Summary: retry exponential backoff jitter delay cap."}])


def test_case3_bot_and_human_sources_both_survive_and_filters_change_candidates(env):
    _bot_and_human_repo(env.gh)

    async def scenario():
        await env.ensure(storage="permanent", pages=3)
        return {
            "both": await env.search(RETRY_QUERY, n_results=15, budget_tokens=16000),
            "narrow": await env.search(RETRY_QUERY, n_results=3),
            "no_bots": await env.search(RETRY_QUERY, n_results=3, exclude_bots=True),
            "bots_only": await env.search(RETRY_QUERY, n_results=15, source_kinds=["bot_review_thread"]),
            "humans_only": await env.search(RETRY_QUERY, n_results=15, source_kinds=["human_review_thread"]),
            "summaries": await env.search(RETRY_QUERY, n_results=15,
                                          source_kinds=["bot_review_summary", "human_review_summary"]),
        }

    r = run(scenario())

    # Both source kinds survive into the results and are labelled honestly.
    kinds = {u["source_kind"] for u in r["both"]["results"]}
    assert {"human_review_thread", "bot_review_thread", "human_review_summary", "bot_review_summary"} <= kinds
    by_kind = {u["source_kind"]: u for u in r["both"]["results"]}
    assert by_kind["human_review_thread"]["author_type"] == "human" and by_kind["human_review_thread"]["is_bot"] is False
    assert by_kind["bot_review_thread"]["author_type"] == "bot" and by_kind["bot_review_thread"]["is_bot"] is True
    assert {u["author"] for u in r["both"]["results"] if u["source_kind"] == "bot_review_thread"} == {
        "github-actions[bot]", "coderabbitai"}  # bot detected via __typename, not just the login
    assert any("Bot-authored evidence is present" in w for w in r["both"]["warnings"])

    # Without filters the five bot threads crowd the narrow cut; excluding bots changes the candidates.
    assert len(r["narrow"]["results"]) == 3
    assert all(u["is_bot"] for u in r["narrow"]["results"])
    assert len(r["no_bots"]["results"]) == 3
    assert all(not u["is_bot"] and u["author_type"] == "human" for u in r["no_bots"]["results"])
    assert not any(u["source_kind"].startswith("bot_") for u in r["no_bots"]["results"])
    assert not any("Bot-authored" in w for w in r["no_bots"]["warnings"])
    assert {"H1", "H2"} <= {u["evidence_id"].split(":")[0] for u in r["no_bots"]["results"]}

    assert r["bots_only"]["results"] and {u["source_kind"] for u in r["bots_only"]["results"]} == {"bot_review_thread"}
    assert len(r["bots_only"]["results"]) == 5
    assert r["humans_only"]["results"] and {u["source_kind"] for u in r["humans_only"]["results"]} == {"human_review_thread"}
    assert len(r["humans_only"]["results"]) == 2
    assert {u["source_kind"] for u in r["summaries"]["results"]} == {"bot_review_summary", "human_review_summary"}

    # The same filters are honoured by the other tools that expose them.
    async def other_tools():
        patterns = await env.search(RETRY_QUERY, tool="get_team_review_patterns", exclude_bots=True)
        review = await env.search(RETRY_QUERY, tool="review_code_with_history", source_kinds=["bot_review_thread"])
        return patterns, review

    patterns, review = run(other_tools())
    assert patterns["results"] and all(not u["is_bot"] for u in patterns["results"])
    assert review["results"] and {u["source_kind"] for u in review["results"]} == {"bot_review_thread"}


# ═════════════════════════════════════════════════════════════════════════════
# Case 4: a later thread appears to reverse a constraint
# ═════════════════════════════════════════════════════════════════════════════

OLD_TEXT = "We standardize on requests for all outbound HTTP calls, keep using requests with a shared session."
NEW_TEXT = ("Revert that: stop using requests here and use httpx instead of requests, "
            "because requests is no longer allowed in the async client.")
OLD_QUERY = "standardize on requests outbound HTTP calls shared session"
NEW_QUERY = "revert stop using requests use httpx instead no longer allowed async client"


def _reversal_repo(gh):
    gh.add_pr(10, title="HTTP client standard", files=["src/http.py"], created="2024-01-05T10:00:00Z", threads=[
        gh.add_thread("OLD", "src/http.py", [comment("OLDa", OLD_TEXT, author=user("alice"),
                                                     created="2024-01-05T10:30:00Z")])])
    gh.add_pr(20, title="HTTP client change", files=["src/http.py"], created="2024-06-05T10:00:00Z", threads=[
        gh.add_thread("NEW", "src/http.py", [comment("NEWa", NEW_TEXT, author=user("carol"),
                                                     created="2024-06-05T10:30:00Z")])])
    gh.add_pr(30, title="Docs touch", files=["docs/readme.md"], created="2024-07-05T10:00:00Z", threads=[
        gh.add_thread("DOC", "docs/readme.md", [comment("DOCa", "Revert the wording change in the docs, the older "
                                                        "phrasing about HTTP was clearer.", author=user("dave"),
                                                        created="2024-07-05T10:30:00Z")])])


def test_case4_apparent_reversal_returns_both_sources_unresolved_and_never_superseded(env):
    _reversal_repo(env.gh)

    async def scenario():
        await env.ensure(storage="permanent", pages=3)
        return (await env.search(OLD_QUERY, n_results=10), await env.search(NEW_QUERY, n_results=10))

    old_first, new_first = run(scenario())

    for envelope in (old_first, new_first):
        http_units = {u["pr_number"]: u for u in envelope["results"] if u["original_path"] == "src/http.py"}
        assert set(http_units) == {10, 20}  # both dated sources are returned
        assert http_units[10]["created_at"] < http_units[20]["created_at"]
        assert http_units[10]["excerpt"].endswith(OLD_TEXT) or OLD_TEXT in http_units[10]["excerpt"]
        assert NEW_TEXT in http_units[20]["excerpt"]
        conflicts = envelope["candidate_conflicts"]
        assert len(conflicts) == 1  # the docs PR touches a different path and is not paired
        conflict = conflicts[0]
        assert conflict["status"] == "unresolved"
        assert conflict["evidence_ids"] == [http_units[10]["evidence_id"], http_units[20]["evidence_id"]]
        # Repository-controlled paths are data in shared_paths, never part of the guidance sentence.
        assert conflict["shared_paths"] == ["src/http.py"] and "src/http.py" not in conflict["basis"]
        assert "keyword heuristic" in conflict["basis"]
        assert all(u["current_applicability"] == "not_assessed" for u in envelope["results"])
        # Nothing, anywhere in the response, is labelled superseded / overruled.
        text = all_text(envelope).lower()
        assert "superseded" not in text and "overruled" not in text and "supersede" not in text

    # Ordering follows relevance to the query, not recency: each phrasing puts its own thread first.
    def rank(envelope, pr):
        return next(i for i, u in enumerate(envelope["results"]) if u["pr_number"] == pr)

    assert rank(old_first, 10) < rank(old_first, 20)
    assert rank(new_first, 20) < rank(new_first, 10)
    newest_first = sorted(old_first["results"], key=lambda u: u["created_at"], reverse=True)
    assert [u["evidence_id"] for u in newest_first] != evidence_ids(old_first)  # not a recency sort
    # The conflict pair lists the earlier source first regardless of which one ranked first.
    assert old_first["candidate_conflicts"][0]["evidence_ids"] == new_first["candidate_conflicts"][0]["evidence_ids"]

    # Every analysis tool serving these units stays free of verdict labels.
    async def every_tool():
        outputs = {}
        for name, arg in [
            ("semantic_search_reviews", "query"), ("find_similar_errors", "error_message"),
            ("review_code_with_history", "code"), ("get_team_review_patterns", "topic"),
            ("generate_tests", "code"), ("static_analysis", "code"),
            ("suggest_refactors", "code"), ("security_check", "code"),
        ]:
            outputs[name] = await env.tool(name, **{arg: OLD_QUERY})
        return outputs

    outputs = run(every_tool())
    for name, payload in outputs.items():
        text = all_text(payload).lower()
        assert "superseded" not in text, name
        assert "supersede" not in text, name
        assert all(c["status"] == "unresolved" for c in payload["candidate_conflicts"]), name
        assert "untrusted" in payload["content_notice"].lower(), name
    assert outputs["semantic_search_reviews"]["candidate_conflicts"]


# ═════════════════════════════════════════════════════════════════════════════
# Case 5: failures are actionable, old generations keep serving, deletes stick
# ═════════════════════════════════════════════════════════════════════════════

def _small_repo(gh):
    for n in (1, 2, 3):
        gh.add_pr(n, threads=[gh.add_thread(
            f"T{n}", f"src/part_{n}.py",
            [comment(f"C{n}", f"zzcache invalidation must happen before part {n} publishes events.",
                     created=f"2024-03-{n:02d}T11:00:00Z")])], files=[f"src/part_{n}.py"])


async def _published(env):
    await env.ensure(storage="permanent", pages=3)
    stats = await env.stats()
    assert stats["index_state"] == "ready_selected_scope" and stats["generation"] == 1
    served = await env.search("zzcache invalidation before publishes events")
    assert served["results"]
    return stats, served


def _assert_previous_generation_still_serves(before_stats, before_search, after_stats, after_search):
    assert after_stats["generation"] == before_stats["generation"] == after_stats["serving_generation"]
    assert after_stats["total_documents"] == before_stats["total_documents"]
    assert after_stats["coverage"]["pull_requests_indexed"] == before_stats["coverage"]["pull_requests_indexed"]
    assert sorted(evidence_ids(after_search)) == sorted(evidence_ids(before_search))
    assert after_search["generation"] == before_search["generation"]


def test_case5_revoked_authorization_on_refresh_is_actionable_and_old_generation_serves(env):
    _small_repo(env.gh)
    gh = env.gh

    async def scenario():
        before_stats, before_search = await _published(env)
        gh.touch(2, "2024-05-01T00:00:00Z")
        gh.error = PermissionError("GitHub rejected the saved credential. Reconnect GitHub and try again.")
        message = await env.ensure(refresh=True, pages=1)
        return before_stats, before_search, message, await env.stats(), await env.search("zzcache invalidation")

    before_stats, before_search, message, stats, search = run(scenario())

    assert "Refresh started" in message  # the failure is reported by state, not by the start message
    assert stats["index_state"] == "stale"
    assert stats["index_job"]["status"] == "failed" and stats["index_job"]["error_kind"] == "authorization_revoked"
    assert stats["last_job"]["error_kind"] == "authorization_revoked"
    assert "authoriz" in stats["stale_reason"].lower() or "authorization_revoked" in stats["stale_reason"]
    assert "github" in stats["action"].lower() and "authoriz" in stats["action"].lower()
    _assert_previous_generation_still_serves(before_stats, before_search, stats, search)
    # The search envelope carries the same actionable state rather than a clean answer.
    assert search["index_state"] == "stale" and search["results"]
    assert "authoriz" in search["action"].lower()
    assert any("stale" in w.lower() and "authorization_revoked" in w for w in search["warnings"])

    # The state is persisted: it survives a restart, then heals after a successful refresh.
    env.restart()
    gh.error = None

    async def recover():
        persisted = await env.stats()
        await env.ensure(refresh=True, pages=3)  # enough pages to finish (page size is 2 PRs)
        return persisted, await env.stats()

    persisted, healed = run(recover())
    assert persisted["index_state"] == "stale" and persisted["last_job"]["error_kind"] == "authorization_revoked"
    assert healed["index_state"] == "ready_selected_scope" and healed["generation"] == 2
    assert healed["stale_reason"] is None and healed["action"] is None


def test_case5_missing_authorization_never_looks_like_an_empty_history_answer(env):
    _small_repo(env.gh)
    env.token_error = GitHubAuthorizationRequired("GitHub is not connected.")

    async def scenario():
        message = await env.ensure(storage="permanent", pages=1)
        return message, await env.stats(), await env.search("zzcache invalidation")

    message, stats, search = run(scenario())
    assert "begin_github_authorization" in message and "error_kind: authorization_required" in message
    assert env.gh.calls == []  # no GitHub traffic without a credential
    assert stats["index_state"] == "failed"
    assert stats["last_job"]["error_kind"] == "authorization_required"
    assert "begin_github_authorization" in stats["action"]
    assert search["index_state"] == "failed" and search["results"] == [] and search["candidate_conflicts"] == []
    joined = " ".join(search["warnings"])
    assert "authorization_required" in joined and "NOT evidence" in joined
    assert not any("No indexed evidence matched" in w for w in search["warnings"])
    assert "begin_github_authorization" in search["action"]
    assert env.collections() == []


def test_case5_failed_refresh_keeps_previous_generation_and_names_the_failure(env):
    _small_repo(env.gh)
    gh = env.gh

    async def scenario():
        before_stats, before_search = await _published(env)
        gh.touch(3, "2024-05-01T00:00:00Z")
        gh.error = RateLimitError(1_900_000_000)
        await env.ensure(refresh=True, pages=1)
        rate_limited = await env.stats()
        gh.error = ValueError("GitHub GraphQL errors: [boom]")
        await env.ensure(refresh=True, pages=1)
        unknown = await env.stats()
        return before_stats, before_search, rate_limited, unknown, await env.search("zzcache invalidation")

    before_stats, before_search, rate_limited, unknown, search = run(scenario())

    assert rate_limited["index_state"] == "stale"
    assert rate_limited["index_job"]["error_kind"] == "rate_limited"
    assert "rate limit" in rate_limited["action"].lower()
    assert unknown["index_state"] == "stale"
    assert unknown["index_job"]["error_kind"] == "unknown" and unknown["action"]
    _assert_previous_generation_still_serves(before_stats, before_search, unknown, search)
    assert search["index_state"] == "stale" and search["stale_reason"]
    assert len(env.collections()) == 1  # no staged leftovers


def test_case5_missing_embedding_files_fail_refresh_actionably_and_search_degrades_honestly(env):
    _small_repo(env.gh)
    gh = env.gh

    async def build():
        return await _published(env)

    before_stats, before_search = run(build())

    def broken_model():
        raise OSError("We couldn't connect to 'https://huggingface.co' to load all-MiniLM-L6-v2")

    # The model files disappear: route every encode through the REAL encoder with no model.
    env.monkeypatch.setattr(real_encoder, "_get_model", broken_model)
    env.monkeypatch.setattr(real_encoder, "_model", None)
    env.monkeypatch.setattr(vs, "encode", real_encoder.encode)
    env.monkeypatch.setattr(vs, "encode_batch", real_encoder.encode_batch)
    env.monkeypatch.setattr(retrieval, "encode", real_encoder.encode)
    gh.touch(1, "2024-05-01T00:00:00Z", body="Now with a changelog entry about the cache.")

    async def scenario():
        await env.ensure(refresh=True, pages=1)
        return await env.stats(), await env.search("zzcache invalidation before publishes events")

    stats, search = run(scenario())

    assert stats["index_state"] == "stale"
    assert stats["index_job"]["error_kind"] == "embeddings_unavailable"
    assert "embedding" in stats["action"].lower() or "model" in stats["action"].lower()
    assert len(env.collections()) == 1  # the staged generation was removed, nothing half-written
    assert stats["generation"] == before_stats["generation"] == 1
    assert stats["total_documents"] == before_stats["total_documents"]
    # Search keeps working from the previous generation, but says it is lexical only.
    assert search["retrieval_mode"] == "lexical_degraded"
    assert any("Semantic ranking is unavailable" in w for w in search["warnings"])
    assert search["results"] and set(evidence_ids(search)) <= set(evidence_ids(before_search))
    assert all(u["match"] in {"exact", "lexical"} for u in search["results"])
    assert search["index_state"] == "stale" and search["action"]


def test_case5_missing_embedding_files_on_first_import_leave_no_generation(env):
    _small_repo(env.gh)

    def broken_model():
        raise OSError("model files are missing")

    env.monkeypatch.setattr(real_encoder, "_get_model", broken_model)
    env.monkeypatch.setattr(real_encoder, "_model", None)
    env.monkeypatch.setattr(vs, "encode", real_encoder.encode)
    env.monkeypatch.setattr(vs, "encode_batch", real_encoder.encode_batch)
    env.monkeypatch.setattr(retrieval, "encode", real_encoder.encode)

    async def scenario():
        message = await env.ensure(storage="permanent", pages=3)
        return message, await env.stats(), await env.search("zzcache invalidation")

    message, stats, search = run(scenario())
    assert "Background indexing started" in message
    assert stats["index_state"] == "failed" and stats["generation"] == 0 and stats["serving_generation"] is None
    assert stats["index_job"]["error_kind"] == "embeddings_unavailable" and stats["action"]
    assert search["index_state"] == "failed" and search["results"] == []
    assert "NOT evidence" in " ".join(search["warnings"]) and search["action"]
    assert env.collections() == []
    assert vs.repo_is_indexed_permanently(REPO) is False


async def _wait_for_list_call(env, count):
    for _ in range(200):
        if len(env.gh.list_calls()) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the indexing job never issued its fetch")


def test_case5_delete_during_running_initial_job_cannot_resurrect_the_index(env):
    _small_repo(env.gh)

    async def scenario():
        gate = env.gh.block()
        await env.ensure(wait=False, storage="permanent", pages=3)
        await _wait_for_list_call(env, 1)
        running = await env.stats()
        await env.mcp.tools["delete_repo_index"](repo=REPO, storage="both", ctx=object())
        gate.set()  # the old job's fetch now returns real data and tries to publish
        await env.drain()
        return running, await env.stats(), await env.search("zzcache invalidation")

    running, stats, search = run(scenario())

    assert running["index_state"] == "indexing"
    assert stats["index_state"] == "no_index"
    assert stats["total_documents"] == 0 and stats["generation"] == 0 and stats["serving_generation"] is None
    assert stats["index_job"]["status"] == "cancelled"
    assert env.collections() == []  # no collection of any generation exists
    assert vs.repo_is_indexed_permanently(REPO) is False
    registry = env.registry()
    assert registry["active_generation"] == 0 and registry["last_job_status"] is None
    assert env.store.get_refresh_state(REPO)["last_updated_at"] in (None, "")
    assert search["index_state"] == "no_index" and search["results"] == []
    assert "ensure_repo_ready" in search["action"]


def test_case5_delete_during_running_refresh_cannot_resurrect_the_old_index_either(env):
    _small_repo(env.gh)
    gh = env.gh

    async def scenario():
        await _published(env)
        gh.touch(2, "2024-05-01T00:00:00Z", body="Changed description.")
        gate = gh.block()
        await env.ensure(wait=False, refresh=True, pages=1)
        await _wait_for_list_call(env, 2)
        message = await env.mcp.tools["delete_repo_index"](repo=REPO, storage="both", ctx=object())
        gate.set()
        await env.drain()
        return message, await env.stats()

    message, stats = run(scenario())
    assert "Deleted index" in message
    assert stats["index_state"] == "no_index"
    assert stats["total_documents"] == 0 and stats["generation"] == 0
    assert env.collections() == []
    assert env.registry()["active_generation"] == 0
    assert vs.repo_is_indexed_permanently(REPO) is False

    # Only a fresh, explicit request builds the index again (new epoch, generation restarts at 1).
    async def rebuild():
        await env.ensure(storage="permanent", pages=3)
        return await env.stats()

    rebuilt = run(rebuild())
    assert rebuilt["index_state"] == "ready_selected_scope" and rebuilt["generation"] == 1
    assert len(env.collections()) == 1
