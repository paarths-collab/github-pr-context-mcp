"""Parallel backfill: plan splitting, chunk claims, chunk completion, progress, cursor and phase switch."""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from entrypoints.local import lessons as lessons_module
from entrypoints.local import live
from entrypoints.local.lessons import LessonError, LessonStore

REPO = "acme/widgets"
BASE = f"https://github.com/{REPO}"


def pr(number, created_at=None):
    created_at = created_at or f"2024-01-{number:02d}T10:00:00Z"
    return {"number": number, "url": f"{BASE}/pull/{number}", "created_at": created_at, "merged": True}


@pytest.fixture
def store(tmp_path):
    return LessonStore(REPO, home=tmp_path)


def chunks_of(plan_result):
    return plan_result["plan"]["chunks"]


# -- plan splitting ------------------------------------------------------------------
def test_plan_splits_oldest_first_into_helper_sized_chunks(store):
    result = store.plan_backfill([9, 3, 1, 7, 5, 2, 8, 4, 6, 10], helpers=3)
    chunks = chunks_of(result)
    assert result["chunk_size"] == 4  # ceil(10 / 3)
    assert [c["prs"] for c in chunks] == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]
    assert [c["chunk_id"] for c in chunks] == ["C1", "C2", "C3"]
    assert all(c["status"] == "open" and c["claimed_at"] is None and c["done_at"] is None for c in chunks)
    assert result["plan"]["plan_id"] == "P1"
    assert store.load()["backfill_plan"] == result["plan"]


def test_plan_drops_processed_and_duplicates(store):
    store.record([pr(2), pr(4)])
    result = store.plan_backfill([1, 2, 3, 4, 5, 5], helpers=2)
    assert result["dropped_processed"] == [2, 4]
    assert [c["prs"] for c in chunks_of(result)] == [[1, 3], [5]]


def test_plan_with_everything_processed_stores_nothing(store):
    store.record([pr(1)])
    result = store.plan_backfill([1])
    assert result["plan"] is None and result["dropped_processed"] == [1]
    assert "backfill_plan" not in store.load()


def test_plan_chunk_size_limits(store):
    numbers = list(range(1, 201))
    assert store.plan_backfill(numbers, helpers=1)["chunk_size"] == 50  # ceil(200/1) capped at 50
    assert len(chunks_of(store.plan_backfill(numbers, helpers=1, replace=True))) == 4
    explicit = store.plan_backfill(numbers, helpers=4, chunk_size=30, replace=True)
    assert explicit["chunk_size"] == 30 and len(chunks_of(explicit)) == 7
    assert [len(c["prs"]) for c in chunks_of(explicit)][-1] == 20
    assert len(chunks_of(store.plan_backfill([1, 2, 3], helpers=16, replace=True))) == 3


@pytest.mark.parametrize("kwargs", [
    {"helpers": 0}, {"helpers": 17}, {"helpers": True}, {"helpers": "4"},
    {"chunk_size": 0}, {"chunk_size": 51}, {"chunk_size": True}, {"replace": "yes"},
])
def test_plan_rejects_bad_limits(store, kwargs):
    with pytest.raises(LessonError):
        store.plan_backfill([1, 2, 3], **kwargs)
    assert "backfill_plan" not in store.load()


@pytest.mark.parametrize("numbers", ["1,2", [0], [-3], [1.5], ["7"], [True], list(range(1, 2002))])
def test_plan_rejects_bad_numbers(store, numbers):
    with pytest.raises(LessonError):
        store.plan_backfill(numbers)


def test_replace_guard_for_unfinished_plans(store):
    store.plan_backfill([1, 2, 3, 4], helpers=2)
    with pytest.raises(LessonError, match="replace"):
        store.plan_backfill([5, 6])
    assert [c["prs"] for c in store.load()["backfill_plan"]["chunks"]] == [[1, 2], [3, 4]]
    replaced = store.plan_backfill([5, 6], helpers=1, replace=True)
    assert replaced["plan"]["plan_id"] == "P2" and chunks_of(replaced)[0]["prs"] == [5, 6]


def test_finished_plan_can_be_replaced_without_flag(store):
    store.plan_backfill([1, 2], helpers=1)
    store.record([pr(1), pr(2)], chunk_id="C1")
    assert store.plan_backfill([3, 4])["plan"]["plan_id"] == "P2"


# -- claims --------------------------------------------------------------------------
def test_claim_takes_next_open_or_a_specific_chunk(store):
    store.plan_backfill([1, 2, 3, 4, 5, 6], helpers=3)
    first = store.claim_chunk()
    assert first["chunk"]["chunk_id"] == "C1" and first["chunk"]["status"] == "claimed"
    assert first["chunk"]["claimed_at"] and first["chunk"]["prs"] == [1, 2]
    assert store.claim_chunk("C3")["chunk"]["chunk_id"] == "C3"
    assert store.claim_chunk()["chunk"]["chunk_id"] == "C2"
    nothing = store.claim_chunk()
    assert nothing["chunk"] is None and "No open chunk" in nothing["note"]


def test_claim_errors(store):
    with pytest.raises(LessonError, match="no backfill plan"):
        store.claim_chunk()
    store.plan_backfill([1, 2], helpers=2)
    store.claim_chunk("C1")
    with pytest.raises(LessonError, match="claimed by another"):
        store.claim_chunk("C1")
    with pytest.raises(LessonError, match="unknown chunk"):
        store.claim_chunk("C9")
    with pytest.raises(LessonError, match="chunk_id"):
        store.claim_chunk("../etc")
    store.record([pr(1)], chunk_id="C1")
    with pytest.raises(LessonError, match="already done"):
        store.claim_chunk("C1")


def test_claims_are_atomic_across_threads(tmp_path):
    LessonStore(REPO, home=tmp_path).plan_backfill(list(range(1, 25)), helpers=8)  # 8 chunks of 3
    claimed, errors = [], []
    barrier = threading.Barrier(16)

    def helper():
        local = LessonStore(REPO, home=tmp_path)
        barrier.wait()
        try:
            chunk = local.claim_chunk()["chunk"]
            (claimed if chunk else errors).append(chunk and chunk["chunk_id"])
        except LessonError as error:  # pragma: no cover - should not happen without a chunk id
            errors.append(str(error))

    threads = [threading.Thread(target=helper) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    won = [c for c in claimed if c]
    assert sorted(won) == [f"C{i}" for i in range(1, 9)]  # every chunk claimed exactly once
    assert len(errors) == 8  # the other 8 helpers found nothing left


def test_same_chunk_race_has_a_single_winner(tmp_path):
    LessonStore(REPO, home=tmp_path).plan_backfill([1, 2], helpers=1)
    results = []
    barrier = threading.Barrier(8)

    def helper():
        barrier.wait()
        try:
            LessonStore(REPO, home=tmp_path).claim_chunk("C1")
            results.append("won")
        except LessonError:
            results.append("lost")

    threads = [threading.Thread(target=helper) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("won") == 1 and results.count("lost") == 7


def _age_claim(store, chunk_id, hours):
    state = store.load()
    chunk = next(c for c in state["backfill_plan"]["chunks"] if c["chunk_id"] == chunk_id)
    old = datetime.now(timezone.utc) - timedelta(hours=hours)
    chunk["claimed_at"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    store._save(state)


def test_stale_claim_can_be_taken_over_after_two_hours(store):
    store.plan_backfill([1, 2, 3, 4], helpers=2)
    store.claim_chunk("C1")
    store.claim_chunk("C2")
    _age_claim(store, "C1", 1.5)
    with pytest.raises(LessonError, match="claimed by another"):
        store.claim_chunk("C1")
    assert store.backfill_progress()["stale_claims"] == []
    _age_claim(store, "C1", 2.5)
    assert store.backfill_progress()["stale_claims"] == ["C1"]
    taken = store.claim_chunk()  # no open chunk: the stale one is offered
    assert taken["chunk"]["chunk_id"] == "C1"
    assert store.backfill_progress()["stale_claims"] == []
    _age_claim(store, "C2", 3)
    assert store.claim_chunk("C2")["chunk"]["status"] == "claimed"


def test_open_chunks_are_preferred_over_stale_ones(store):
    store.plan_backfill([1, 2, 3, 4], helpers=2)
    store.claim_chunk("C1")
    _age_claim(store, "C1", 5)
    assert store.claim_chunk()["chunk"]["chunk_id"] == "C2"


# -- chunk completion ----------------------------------------------------------------
def test_full_chunk_is_marked_done(store):
    store.plan_backfill([1, 2, 3, 4], helpers=2)
    store.claim_chunk("C1")
    result = store.record([pr(1), pr(2)], chunk_id="C1")
    assert result["chunk"] == {"chunk_id": "C1", "status": "done", "missing_prs": []}
    chunk = store.load()["backfill_plan"]["chunks"][0]
    assert chunk["status"] == "done" and chunk["done_at"]


def test_partial_chunk_stays_claimed_and_reports_missing(store):
    store.plan_backfill([1, 2, 3, 4], helpers=2)
    store.claim_chunk("C1")
    partial = store.record([pr(1)], chunk_id="C1")
    assert partial["chunk"] == {"chunk_id": "C1", "status": "claimed", "missing_prs": [2]}
    assert store.load()["backfill_plan"]["chunks"][0]["done_at"] is None
    finished = store.record([pr(2)], chunk_id="C1")
    assert finished["chunk"]["status"] == "done" and finished["chunk"]["missing_prs"] == []


def test_recording_an_unclaimed_partial_chunk_claims_it(store):
    store.plan_backfill([1, 2], helpers=1)
    assert store.record([pr(1)], chunk_id="C1")["chunk"]["status"] == "claimed"


def test_chunk_completes_when_prs_were_processed_by_other_calls(store):
    store.plan_backfill([1, 2], helpers=1)
    store.record([pr(1), pr(2)])  # recorded without a chunk id
    assert store.record([], chunk_id="C1")["chunk"]["status"] == "done"


def test_unknown_chunk_or_missing_plan_changes_nothing(store):
    with pytest.raises(LessonError, match="unknown chunk"):
        store.record([pr(1)], chunk_id="C1")
    store.plan_backfill([1, 2], helpers=1)
    with pytest.raises(LessonError, match="unknown chunk"):
        store.record([pr(1)], chunk_id="C2")
    with pytest.raises(LessonError, match="chunk_id"):
        store.record([pr(1)], chunk_id="x")
    assert store.load()["processed_numbers"] == []


def test_observations_with_chunk_id_follow_the_normal_pending_flow(store):
    store.plan_backfill([3], helpers=1)
    observation = {"pr": 3, "url": f"{BASE}/pull/3#discussion_r1", "date": "2024-01-03",
                   "kind": "change_request_fixed", "reviewer": "alice", "author": "bob",
                   "paths": ["src/api/client.py"], "rule": "Pass timeout= to requests.get in the API client",
                   "why": "A hung request blocked the worker.", "quote": "Please add a timeout to this call."}
    result = store.record([pr(3)], [], [], False, [observation], chunk_id="C1")
    assert result["chunk"]["status"] == "done" and len(result["pending_created"]) == 1
    assert store.get_lessons()["lessons"] == []  # helpers never create rules: the checker must approve


# -- progress ------------------------------------------------------------------------
def test_progress_without_a_plan(store):
    progress = store.backfill_progress()
    assert progress["plan_id"] is None and progress["ready_for_checker"] is False


def test_progress_counts_chunks_prs_and_readiness(store):
    store.plan_backfill([1, 2, 3, 4, 5, 6], helpers=3)
    progress = store.backfill_progress()
    assert progress["chunks"] == {"open": 3, "claimed": 0, "done": 0}
    assert progress["prs_total"] == 6 and progress["prs_left"] == 6 and progress["ready_for_checker"] is False
    store.claim_chunk("C1")
    store.claim_chunk("C2")
    store.record([pr(3), pr(4)], chunk_id="C2")
    progress = store.backfill_progress()
    assert progress["chunks"] == {"open": 1, "claimed": 1, "done": 1}
    assert progress["prs_left"] == 4 and progress["ready_for_checker"] is False
    store.record([pr(1), pr(2)], chunk_id="C1")
    store.record([pr(5), pr(6)], chunk_id="C3")
    progress = store.backfill_progress()
    assert progress["chunks"] == {"open": 0, "claimed": 0, "done": 3}
    assert progress["prs_left"] == 0 and progress["ready_for_checker"] is True and progress["phase"] == "backfill"


def test_progress_reports_pending_checks(store):
    store.plan_backfill([3], helpers=1)
    observation = {"pr": 3, "url": f"{BASE}/pull/3#discussion_r1", "date": "2024-01-03",
                   "kind": "revert", "reviewer": "alice", "author": "bob", "paths": ["src/a.py"],
                   "rule": "Do not remove the retry loop; keep it", "why": "It broke uploads.", "quote": ""}
    store.record([pr(3)], [], [], False, [observation], chunk_id="C1")
    assert store.backfill_progress()["pending_checks"] == 1


# -- out-of-order chunks, cursor and phase switch --------------------------------------
def test_out_of_order_chunks_keep_cursor_and_counts_right(tmp_path):
    prs = [pr(n) for n in range(1, 9)]
    ordered = LessonStore("acme/ordered", home=tmp_path)
    shuffled = LessonStore("acme/shuffled", home=tmp_path)
    ordered.plan_backfill(list(range(1, 9)), helpers=2)
    shuffled.plan_backfill(list(range(1, 9)), helpers=2)

    def fix(store, p):
        slug = store.repo
        return {**p, "url": f"https://github.com/{slug}/pull/{p['number']}"}

    ordered.record([fix(ordered, p) for p in prs[:4]], chunk_id="C1")
    ordered.record([fix(ordered, p) for p in prs[4:]], chunk_id="C2")
    shuffled.record([fix(shuffled, p) for p in prs[4:]], chunk_id="C2")  # newest chunk first
    assert shuffled.load()["backfill_cursor"]["number"] == 8
    shuffled.record([fix(shuffled, p) for p in prs[:4]], chunk_id="C1")  # then the older one

    a, b = ordered.load(), shuffled.load()
    for key in ("processed_numbers", "processed_count", "phase"):
        assert a[key] == b[key]
    for key in ("backfill_cursor", "newest_processed"):
        assert (a[key]["number"], a[key]["created_at"]) == (b[key]["number"], b[key]["created_at"])
    assert b["backfill_cursor"]["number"] == 8 and b["processed_count"] == 8
    assert b["processed_numbers"] == list(range(1, 9)) and b["phase"] == "backfill"
    assert shuffled.backfill_progress()["ready_for_checker"] is True


def test_reprocessing_a_pr_in_another_chunk_does_not_double_count(store):
    store.plan_backfill([1, 2, 3, 4], helpers=2)
    store.record([pr(1), pr(2)], chunk_id="C1")
    again = store.record([pr(2), pr(3), pr(4)], chunk_id="C2")
    assert again["processed_skipped"] == [2] and again["chunk"]["status"] == "done"
    assert store.load()["processed_count"] == 4


def test_phase_switches_only_after_all_chunks_are_done(store):
    store.plan_backfill([1, 2, 3, 4], helpers=2)
    store.record([pr(1), pr(2)], chunk_id="C1")
    refused = store.record([], [], [], True)
    assert "backfill_complete_refused" in refused and refused["phase"] == "backfill"
    assert store.load()["backfill_completed_at"] is None
    store.record([pr(3), pr(4)], chunk_id="C2")
    done = store.record([], [], [], True)
    assert "backfill_complete_refused" not in done and done["phase"] == "ongoing"
    assert store.load()["backfill_completed_at"]
    assert store.status()["phase"] == "ongoing"


def test_backfill_complete_without_a_plan_still_works(store):
    assert store.record([pr(1)], [], [], True)["phase"] == "ongoing"


# -- MCP registration ------------------------------------------------------------------
def call(name, **args):
    result = asyncio.run(live.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_tools_and_prompts_are_registered():
    tools = {t.name: t for t in asyncio.run(live.mcp.list_tools())}
    assert {"plan_backfill", "claim_chunk", "backfill_progress"} <= set(tools)
    assert "chunk_id" in tools["record_learning"].inputSchema["properties"]
    assert tools["plan_backfill"].inputSchema["required"] == ["repo", "pr_numbers"]
    prompts = {p.name for p in asyncio.run(live.mcp.list_prompts())}
    assert {"learn_history_parallel", "learn_history_chunk"} <= prompts


def test_tools_work_end_to_end_through_fastmcp(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_PR_CONTEXT_HOME", str(tmp_path))
    plan = call("plan_backfill", repo=REPO, pr_numbers=[1, 2, 3, 4], helpers=2)
    assert plan["ok"] and [c["prs"] for c in plan["plan"]["chunks"]] == [[1, 2], [3, 4]]
    assert call("plan_backfill", repo=REPO, pr_numbers=[5])["ok"] is False  # replace guard surfaces as an error
    claim = call("claim_chunk", repo=REPO)
    assert claim["chunk"]["chunk_id"] == "C1"
    done = call("record_learning", repo=REPO, processed_prs=[pr(1), pr(2)], chunk_id="C1")
    assert done["chunk"]["status"] == "done"
    assert call("claim_chunk", repo=REPO, chunk_id="C1")["ok"] is False
    progress = call("backfill_progress", repo=REPO)
    assert progress["chunks"] == {"open": 1, "claimed": 0, "done": 1} and progress["ready_for_checker"] is False
    assert call("record_learning", repo=REPO, processed_prs=[], chunk_id="C9")["ok"] is False


def test_prompts_carry_the_workflow_and_untrusted_data_warning():
    async def prompt(name, args):
        return (await live.mcp.get_prompt(name, args)).messages[0].content.text

    manager = asyncio.run(prompt("learn_history_parallel", {"repo": REPO}))
    for word in ("plan_backfill", "backfill_progress", "sub-agent", "one after another", "check_pending_rules",
                 "backfill_complete", "untrusted"):
        assert word in manager
    assert REPO in manager
    helper = asyncio.run(prompt("learn_history_chunk", {"repo": REPO, "chunk_id": "C2"}))
    for word in ("claim_chunk", "chunk_id", "get_review_comments", "quote", "principle", "family", "untrusted",
                 "Never call submit_checks"):
        assert word in helper
    assert '"C2"' in helper and REPO in helper
    assert lessons_module.CLAIM_STALE == timedelta(hours=2)
