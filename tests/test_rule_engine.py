"""Rule engine: observations, checker gate, lifecycle, score, scope, serving, export, migration."""

import asyncio
import json
import sys
import threading
from unittest.mock import patch

import pytest

from entrypoints.local import live, rules
from entrypoints.local.lessons import LessonError, LessonStore

REPO = "acme/widgets"
BASE = f"https://github.com/{REPO}"
RULE = "Never retry non-idempotent calls"


def pr(number, created_at="2024-01-10T10:00:00Z"):
    return {"number": number, "url": f"{BASE}/pull/{number}", "created_at": created_at, "merged": True}


def obs(number=3, **extra):
    item = {"pr": number, "url": f"{BASE}/pull/{number}#discussion_r1", "date": "2024-01-03",
            "kind": "change_request_fixed", "reviewer": "alice", "author": "bob",
            "paths": ["src/api/client.py"], "rule": RULE, "why": "Duplicate charges happened.",
            "quote": "Please do not retry POST requests."}
    item.update(extra)
    return item


def repeat_obs(number, lesson_id="L1", **extra):
    return obs(number, lesson_id=lesson_id, **extra)


@pytest.fixture
def store(tmp_path):
    return LessonStore(REPO, home=tmp_path)


def rec(store, *observations, prs=(), complete=False):
    return store.record(list(prs), [], [], complete, list(observations))


def decide(store, verdict="approve", **extra):
    checks = store.list_pending_checks()["checks"]
    return store.submit_checks([{"check_id": c["check_id"], "verdict": verdict, "reason": "checked", **extra}
                                for c in checks])


def learn(store, *observations, **kwargs):
    """Record observations, then approve everything the checker sees."""
    rec(store, *observations, **kwargs)
    return decide(store)


def lesson_of(store, lesson_id="L1"):
    return next(x for x in store.get_lessons()["lessons"] if x["id"] == lesson_id)


def active_lesson(store, rule=RULE, base=3, **extra):
    """An active lesson: strong evidence in two distinct PRs from two reviewers."""
    learn(store, obs(base, rule=rule, **extra), obs(base + 1, rule=rule, reviewer="carol", **extra))


# -- observations: code-enforced discards ---------------------------------------
@pytest.mark.parametrize("extra,reason", [
    ({"kind": "review_comment"}, "weak"),
    ({"reviewer": "Bob"}, "author"),
    ({"reviewer": "dependabot[bot]"}, "bot"),
    ({"reviewer": "github-actions"}, "bot"),
    ({"reviewer": "lint-bot"}, "bot"),
    ({"quote": ""}, "missing quote"),
    ({"quote": "  \n "}, "missing quote"),
    ({"url": "https://github.com/other/repo/pull/3"}, "must be"),
    ({"url": f"{BASE}/pull/4"}, "does not point"),
    ({"url": "https://evil.example/acme/widgets/pull/3"}, "must be"),
])
def test_discard_rules(store, extra, reason):
    result = rec(store, obs(3, **extra))
    assert len(result["discarded"]) == 1 and reason in result["discarded"][0]["reason"]
    assert result["pending_created"] == [] and store.list_pending_checks()["count"] == 0
    status = store.status()
    weak = extra.get("kind") == "review_comment"
    assert (status["weak_comments"], status["discarded"]) == ((1, 0) if weak else (0, 1))


@pytest.mark.parametrize("extra", [
    {"reviewer": "dependabot[bot]", "kind": "follow_up_fix"},
    {"reviewer": "renovate-bot", "kind": "revert", "quote": ""},
    {"quote": "", "kind": "revert"},
])
def test_bots_may_flag_reverts_and_fixes_and_reverts_need_no_quote(store, extra):
    result = rec(store, obs(3, **extra))
    assert result["discarded"] == [] and result["pending_created"][0]["type"] == "new_lesson"


def test_new_lesson_needs_a_why(store):
    assert "needs a why" in rec(store, obs(3, why=""))["discarded"][0]["reason"]


def test_bad_shapes_are_rejected_without_writing(store):
    for bad in (obs(3, kind="opinion"), obs(3, paths="src"), obs(3, reviewer=""), obs(3, lesson_id="../L1"),
                obs(3, example="x"), obs(3, date="yesterday"), "x", obs("3")):
        with pytest.raises(LessonError):
            rec(store, bad)
    assert not store.dir.exists()


def test_quote_example_and_paths_are_cleaned_and_capped(store):
    rec(store, obs(3, quote="q" * 900 + "\x1b", paths=["./src\\api//x.py", "../etc/passwd", "", 5],
                   example={"bad": "a" * 900, "good": "if x:\r\n\ty = 1\x07\n"}))
    check = store.list_pending_checks()["checks"][0]["evidence"][0]
    assert len(check["quote"]) == 300 and "\x1b" not in check["quote"]
    assert check["paths"] == ["src/api/x.py"]
    assert len(check["example"]["bad"]) == 600
    assert check["example"]["good"] == "if x:\n    y = 1"


# -- same PR counts once ----------------------------------------------------------
def test_same_pr_and_lesson_counts_once(store):
    rec(store, obs(3), obs(3, quote="again, same reviewer"), obs(3, kind="follow_up_fix"))
    assert len(store.list_pending_checks()["checks"]) == 1
    decide(store)
    got = lesson_of(store)
    assert len(got["evidence"]) == 1 and got["status"] == "candidate"
    # A second reviewer on the same PR is not a second PR: still a candidate, but it informs team/preference.
    rec(store, obs(3, reviewer="carol"))
    got = lesson_of(store)
    assert got["status"] == "candidate" and got["kind"] == "team" and len(got["evidence"]) == 2
    # Re-recording the identical observation changes nothing.
    again = rec(store, obs(3, reviewer="carol"))
    assert again["evidence_skipped"] and len(lesson_of(store)["evidence"]) == 2


def test_stronger_kind_on_same_pr_upgrades_in_place(store):
    learn(store, obs(3))
    result = rec(store, obs(3, kind="revert", quote=""))
    assert result["evidence_added"] and lesson_of(store)["status"] == "active"
    assert len(lesson_of(store)["evidence"]) == 1


# -- pending gate -----------------------------------------------------------------
def test_nothing_counts_before_approval(store):
    rec(store, obs(3, kind="revert", quote=""), obs(4, kind="revert", quote=""), complete=True)
    assert store.get_lessons()["lessons"] == []
    assert store.get_relevant_lessons(["src/api/client.py"])["lessons"] == []
    assert store.list_alerts()["alerts"] == [] and store.status()["lessons_active"] == 0
    assert store.status()["pending_checks"] == 1
    assert "1 pending check" in (store.dir / "LESSONS.md").read_text()
    # The repeat claim on an active lesson is also gated.
    decide(store)
    rec(store, repeat_obs(9))
    assert lesson_of(store)["repeat_count"] == 0 and lesson_of(store)["status"] == "active"
    assert store.list_alerts()["alerts"] == []


def test_pending_checks_show_only_rule_quote_and_evidence(store):
    active_lesson(store)
    rec(store, repeat_obs(9, rule="Do not retry POSTs", why="learner reasoning that must stay hidden"))
    checks = store.list_pending_checks()["checks"]
    assert {c["type"] for c in checks} == {"repeat", "rule_edit"}
    dump = json.dumps(checks)
    assert "learner reasoning" not in dump and '"why"' not in dump
    repeat = next(c for c in checks if c["type"] == "repeat")
    assert repeat["current_rule"] == RULE and repeat["evidence"][0]["quote"].startswith("Please do not")
    assert set(repeat) == {"check_id", "type", "lesson_id", "rule", "evidence", "current_rule"}


def test_reject_is_recorded_and_never_requeued(store):
    rec(store, obs(3))
    result = decide(store, "reject", reason="just taste")
    assert result["applied"][0]["verdict"] == "reject" and store.get_lessons()["lessons"] == []
    state = store.load()
    assert any(v["verdict"] == "reject" and v["reason"] == "just taste" for v in state["decided"].values())
    again = rec(store, obs(3))
    assert again["pending_created"] == [] and "already rejected" in again["discarded"][0]["reason"]
    assert rec(store, obs(5))["pending_created"]  # another PR is a new claim


def test_rejected_repeat_and_contradiction_are_not_requeued(store):
    active_lesson(store)
    rec(store, repeat_obs(9), obs(10, contradicts="L1", kind="revert", quote=""))
    decide(store, "reject")
    assert lesson_of(store)["repeat_count"] == 0 and store.list_alerts()["alerts"] == []
    for item in (repeat_obs(9), obs(10, contradicts="L1", kind="revert", quote="")):
        result = rec(store, item)
        assert result["pending_created"] == [] and result["discarded"]


def test_submit_checks_validates_each_decision(store):
    rec(store, obs(3))
    cid = store.list_pending_checks()["checks"][0]["check_id"]
    result = store.submit_checks([{"check_id": "C99", "verdict": "approve"}, {"check_id": cid, "verdict": "maybe"},
                                  {"check_id": cid, "verdict": "reject"}, {"check_id": cid, "verdict": "edit"},
                                  "junk", {"check_id": cid, "verdict": "approve"}, {"check_id": cid, "verdict": "approve"}])
    assert len(result["errors"]) == 6 and len(result["applied"]) == 1
    assert store.list_pending_checks()["count"] == 0 and len(store.get_lessons()["lessons"]) == 1
    with pytest.raises(LessonError):
        store.submit_checks("nope")


def test_edit_verdict_replaces_rule_text(store):
    rec(store, obs(3, rule="never retry things"))
    result = decide(store, "edit", rule="Never retry non-idempotent HTTP calls.")
    assert result["new_lessons"][0]["rule"] == "Never retry non-idempotent HTTP calls."
    assert lesson_of(store)["rule"] == "Never retry non-idempotent HTTP calls."
    # An edit that equals an existing rule merges instead of duplicating.
    rec(store, obs(8, rule="something else entirely"))
    assert decide(store, "edit", rule="never retry non-idempotent http calls")["merged_duplicates"] == [
        {"lesson_id": "L1"}]
    assert len(store.get_lessons()["lessons"]) == 1


def test_rule_edit_is_gated_then_applied(store):
    active_lesson(store)
    rec(store, repeat_obs(9, rule="Never retry POST requests"))
    assert lesson_of(store)["rule"] == RULE
    checks = store.list_pending_checks()["checks"]
    edit = next(c for c in checks if c["type"] == "rule_edit")
    store.submit_checks([{"check_id": edit["check_id"], "verdict": "edit", "rule": "Do not retry POSTs"}])
    assert lesson_of(store)["rule"] == "Do not retry POSTs"
    repeat = next(c for c in checks if c["type"] == "repeat")
    err = store.submit_checks([{"check_id": repeat["check_id"], "verdict": "edit", "rule": "x"}])["errors"]
    assert "rule proposals only" in err[0]["reason"]
    assert store.list_pending_checks()["count"] == 1  # the failed edit left it pending


def test_more_evidence_for_approved_lesson_needs_no_recheck(store):
    learn(store, obs(3))
    assert lesson_of(store)["status"] == "candidate"
    result = rec(store, obs(4, reviewer="carol"))
    assert result["pending_created"] == [] and lesson_of(store)["status"] == "active"


def test_same_new_rule_from_several_prs_is_one_check(store):
    result = rec(store, obs(3), obs(4), obs(5, rule="NEVER retry non-idempotent calls."))
    assert len(result["pending_created"]) == 1
    check = store.list_pending_checks()["checks"][0]
    assert [e["pr"] for e in check["evidence"]] == [3, 4, 5]
    decide(store)
    assert lesson_of(store)["status"] == "active"


def test_removing_a_lesson_drops_its_pending_checks_and_ids_are_validated(store):
    active_lesson(store)
    rec(store, repeat_obs(9))
    store.remove_lesson("L1", "wrong")
    assert store.list_pending_checks()["count"] == 0
    assert rec(store, repeat_obs(10))["discarded"][0]["reason"].startswith("unknown or removed")
    assert rec(store, obs(11, contradicts="L9", kind="revert", quote=""))["discarded"]
    assert rec(store, obs(12))["suppressed"] == [{"lesson_id": "L1", "reason": "previously removed"}]


# -- lifecycle --------------------------------------------------------------------
def test_candidate_becomes_active_with_second_distinct_pr(store):
    learn(store, obs(3))
    assert lesson_of(store)["status"] == "candidate"
    assert store.get_relevant_lessons(["src/api/client.py"])["lessons"] == []
    assert [c["id"] for c in store.list_candidates()["candidates"]] == ["L1"]
    rec(store, obs(4, kind="follow_up_fix"))
    assert lesson_of(store)["status"] == "active" and store.list_candidates()["candidates"] == []
    assert [x["id"] for x in store.get_relevant_lessons(["src/api/client.py"])["lessons"]] == ["L1"]


def test_revert_makes_a_lesson_active_at_once(store):
    learn(store, obs(3, kind="revert", quote=""))
    assert lesson_of(store)["status"] == "active"


def test_repeat_on_active_lesson_hardens_and_alerts_in_ongoing_phase(store):
    active_lesson(store)
    rec(store, complete=True)
    rec(store, repeat_obs(9, quote="same retry bug again"))
    result = decide(store)
    alert = result["alerts"][0]
    assert alert["type"] == "repeat" and alert["lesson_id"] == "L1" and alert["pr"] == 9
    assert alert["note"] == "same retry bug again" and "you may want to look at it" in result["tell_user"]
    got = lesson_of(store)
    assert got["status"] == "hardened" and got["hardened"] and got["repeat_count"] == 1
    assert "### L1 (repeated 1x)" in (store.dir / "LESSONS.md").read_text().split("## Rules")[0]


def test_repeat_during_backfill_hardens_quietly(store):
    active_lesson(store)
    rec(store, repeat_obs(9))
    result = decide(store)
    assert result["alerts"] == [] and result["historical_repeats"] == [{"lesson_id": "L1", "pr": 9, "repeat_count": 1}]
    assert lesson_of(store)["status"] == "hardened"
    # The phase when the claim was recorded decides, not when it was checked: a repeat recorded in the
    # batch that completes the backfill is still old history.
    rec(store, repeat_obs(10), complete=True)
    assert store.status()["phase"] == "ongoing"
    rec(store, repeat_obs(11))
    assert decide(store)["alerts"] and [a["pr"] for a in store.list_alerts()["alerts"]] == [11]


def test_manual_approval_and_removal_of_candidates(store):
    learn(store, obs(3))
    assert store.approve_lesson("L1")["status"] == "active"
    assert store.approve_lesson("L1")["status"] == "active"  # idempotent
    learn(store, obs(5, rule="Use a context manager for locks"))
    store.remove_lesson("L2", "not a rule")
    for bad in ("L2", "L9"):
        with pytest.raises(LessonError):
            store.approve_lesson(bad)
    assert [x["id"] for x in store.get_lessons()["lessons"]] == ["L1"]


def test_archived_by_repo_clock_age_and_revival(store):
    learn(store, obs(3, date="2022-12-01"), obs(4, date="2022-12-02", reviewer="carol"),
          prs=[pr(3, "2022-12-01T00:00:00Z")])
    assert lesson_of(store)["status"] == "active"
    rec(store, prs=[pr(50, "2024-06-01T00:00:00Z")])  # the clock moves with processed PRs, not the wall clock
    assert lesson_of(store)["status"] == "archived"
    assert store.get_relevant_lessons(["src/api/client.py"])["lessons"] == []
    assert store.status()["lessons_archived"] == 1 and "## Archived\n\n- L1:" in (store.dir / "LESSONS.md").read_text()
    result = rec(store, obs(51, date="2024-05-30"))  # new approved evidence revives it
    assert result["pending_created"] == [] and lesson_of(store)["status"] == "active"


def test_archive_boundary_is_exactly_365_days(store):
    learn(store, obs(3, kind="revert", quote="", date="2023-06-01"), prs=[pr(3, "2023-06-01T00:00:00Z")])
    rec(store, prs=[pr(4, "2024-05-31T00:00:00Z")])  # 365 days later
    assert lesson_of(store)["status"] == "active"
    rec(store, prs=[pr(5, "2024-06-01T00:00:00Z")])  # 366 days
    assert lesson_of(store)["status"] == "archived"


def test_contradiction_opens_conflict_that_resolve_keep_closes(store):
    active_lesson(store)
    rec(store, obs(9, contradicts="L1", kind="revert", quote="", rule="Retrying is fine behind an idempotency key"))
    check = store.list_pending_checks()["checks"][0]
    assert check["type"] == "contradiction" and check["current_rule"] == RULE
    assert store.list_alerts()["alerts"] == []
    result = decide(store)
    alert = result["alerts"][0]
    assert alert["type"] == "conflict" and alert["resolution"] is None and result["conflicts"] == [alert["id"]]
    assert lesson_of(store)["status"] == "active"  # still served until a person decides
    resolved = store.resolve_conflict(alert["id"], "keep")
    assert resolved["lesson_status"] == "active" and resolved["alert"]["acknowledged"] is True
    assert store.resolve_conflict(alert["id"], "keep")["alert"]["resolution"] == "keep"  # idempotent
    with pytest.raises(LessonError):
        store.resolve_conflict(alert["id"], "archive")


def test_conflict_resolved_archive_removes_from_serving_and_new_evidence_revives(store):
    active_lesson(store)
    rec(store, obs(9, contradicts="L1", kind="revert", quote=""))
    alert = decide(store)["alerts"][0]
    assert store.resolve_conflict(alert["id"], "archive")["lesson_status"] == "archived"
    assert store.get_relevant_lessons(["src/api/client.py"])["lessons"] == []
    rec(store, obs(20, date="2024-02-01"))
    assert lesson_of(store)["status"] == "active"


def test_resolve_conflict_rejects_bad_input(store):
    active_lesson(store)
    rec(store, complete=True)
    rec(store, repeat_obs(9))
    repeat_alert = decide(store)["alerts"][0]
    for args in (("A99", "keep"), (repeat_alert["id"], "keep")):
        with pytest.raises(LessonError):
            store.resolve_conflict(*args)
    with pytest.raises(LessonError):
        store.resolve_conflict(repeat_alert["id"], "maybe")


# -- team vs preference and score ---------------------------------------------------
def test_team_needs_two_distinct_reviewers_and_preference_halves_score(store):
    learn(store, obs(3, kind="revert", quote=""))  # one reviewer
    got = lesson_of(store)
    assert got["kind"] == "preference" and got["score"] == 1.75  # (3 + 0.5) * 0.5
    rec(store, obs(3, kind="revert", quote="", reviewer="carol"))  # second reviewer, same PR
    got = lesson_of(store)
    assert got["kind"] == "team" and got["score"] == 4.0  # 3 + 2 * 0.5
    assert "preference" not in json.dumps(store.get_relevant_lessons(["src/api/x.py"]))
    learn(store, obs(9, rule="Pin the version", reviewer="Dave"), obs(10, rule="Pin the version", reviewer="DAVE"))
    assert lesson_of(store, "L2")["kind"] == "preference"  # reviewer names compare case-insensitively
    assert "reviewer preference" in (store.dir / "LESSONS.md").read_text()


def test_score_formula_exact_numbers(store):
    day = "2024-03-01"
    learn(store, obs(3, kind="revert", quote="", reviewer="alice", date=day),
          obs(4, kind="follow_up_fix", reviewer="dave", date=day), obs(5, reviewer="alice", date=day),
          prs=[pr(57, "2024-03-01T00:00:00Z")])
    # 1 revert + 1 follow-up fix + 1 change request: 3 + 2 + 1, two reviewers: 1.0, no repeats; age 0.
    assert lesson_of(store)["score"] == 7.0 and lesson_of(store)["kind"] == "team"
    rec(store, repeat_obs(6, reviewer="carol", date="2024-03-01"))
    decide(store)
    # +1 for the repeat PR as a change request, +0.5 third reviewer, +2 approved repeat.
    assert lesson_of(store)["score"] == 10.5


def test_score_decays_by_half_per_365_days_of_evidence_age(store):
    learn(store, obs(3, kind="revert", quote="", reviewer="alice", date="2023-03-01"),
          obs(3, kind="revert", quote="", reviewer="carol", date="2023-03-01"),
          prs=[pr(57, "2024-02-29T00:00:00Z")])  # exactly 365 days after the evidence
    assert lesson_of(store)["score"] == 2.0 and lesson_of(store)["status"] == "active"  # (3 + 1.0) * 0.5
    learn(store, obs(8, rule="Pin versions", kind="revert", quote="", date="2023-03-01"))
    assert lesson_of(store, "L2")["score"] == 0.88  # (3 + 0.5) * 0.5 decay * 0.5 preference = 0.875


# -- scope ---------------------------------------------------------------------------
def test_scope_directories_max_depth_and_global():
    assert rules.lesson_scope(["src/api/v1/handlers/x.py", "src/api/y.py", "setup.py"]) == {
        "dirs": ["", "src/api", "src/api/v1"], "global": False, "unscoped": False}
    assert rules.lesson_scope(["a/x.py", "b/c/y.py", "c/z.py", "d.py"])["global"] is True
    assert rules.lesson_scope(["a/x.py", "a/b/y.py", "b/z.py"])["global"] is False
    assert rules.lesson_scope([])["unscoped"] is True


def test_scope_recorded_from_evidence_paths(store):
    learn(store, obs(3, paths=["src/api/v1/handlers/x.py"]), obs(4, paths=["src/db/y.py"], reviewer="carol"))
    assert lesson_of(store)["scope"] == {"dirs": ["src/api/v1", "src/db"], "global": False, "unscoped": False}


# -- serving -------------------------------------------------------------------------
def served_ids(store, paths, **kwargs):
    return [x["id"] for x in store.get_relevant_lessons(paths, **kwargs)["lessons"]]


def test_relevant_lessons_are_filtered_by_scope_and_status(store):
    active_lesson(store, rule="Rule api", paths=["src/api/a.py"], base=3)
    active_lesson(store, rule="Rule docs", paths=["docs/a.md"], base=10)
    learn(store, obs(20, rule="Rule candidate", paths=["src/api/c.py"]))  # L3: candidate
    active_lesson(store, rule="Rule global", paths=["a/x.py", "b/y.py", "c/z.py"], base=40)  # L4
    rec(store, obs(30, rule="Rule unchecked", paths=["src/api/u.py"], kind="revert", quote=""))  # pending only
    assert sorted(served_ids(store, ["src/api/new/file.py"])) == ["L1", "L4"]
    assert sorted(served_ids(store, ["docs/guide/intro.md"])) == ["L2", "L4"]
    assert served_ids(store, ["src/other/file.py"]) == ["L4"]
    assert served_ids(store, []) == ["L4"]  # no paths: only global lessons
    assert sorted(served_ids(store, ["src\\api\\x.py", "../x", 5])) == ["L1", "L4"]  # messy paths are tolerated


def test_relevant_lessons_order_limit_and_omitted(store):
    learn(store, obs(3, rule="Rule A", paths=["src/a.py"]), obs(4, rule="Rule A", paths=["src/a.py"]))
    active_lesson(store, rule="Rule B", paths=["src/b.py"], base=10, kind="follow_up_fix")
    learn(store, obs(20, rule="Rule C", paths=["src/c.py"], kind="revert", quote=""),
          obs(21, rule="Rule C", paths=["src/c.py"], kind="revert", quote=""))
    rec(store, repeat_obs(30, lesson_id="L1"), complete=True)
    decide(store)  # L1 hardened with the lowest score of the three
    result = store.get_relevant_lessons(["src/x.py"])
    order = [x["id"] for x in result["lessons"]]
    scores = {x["id"]: x["score"] for x in result["lessons"]}
    assert order[0] == "L1" and scores["L1"] < scores["L3"]  # hardened first, then by score
    assert order[1:] == sorted(order[1:], key=lambda i: -scores[i])
    assert result["lessons"][0]["status"] == "hardened"
    limited = store.get_relevant_lessons(["src/x.py"], limit=2)
    assert len(limited["lessons"]) == 2 and limited["omitted"] == 1 and limited["matched"] == 3
    assert store.get_relevant_lessons(["src/x.py"], limit=0)["limit"] == 1
    assert store.get_relevant_lessons(["src/x.py"], limit=999)["limit"] == 20
    for bad in ({"limit": "3"}, {"limit": True}):
        with pytest.raises(LessonError):
            store.get_relevant_lessons(["src/x.py"], **bad)
    with pytest.raises(LessonError):
        store.get_relevant_lessons("src/x.py")


def test_compact_shape_and_best_link(store):
    learn(store, obs(3, kind="change_request_fixed", why="w" * 500),
          obs(4, kind="follow_up_fix", date="2024-02-01"), obs(5, kind="revert", quote="", date="2024-01-01"),
          obs(6, kind="follow_up_fix", date="2024-03-01"))
    entry = store.get_relevant_lessons(["src/api/z.py"])["lessons"][0]
    assert set(entry) == {"id", "rule", "why", "status", "kind", "score", "link"}
    assert len(entry["why"]) == 160 and entry["status"] == "active" and entry["kind"] == "preference"
    assert entry["link"] == f"{BASE}/pull/5#discussion_r1"  # revert beats newer fixes
    store.remove_lesson("L1", "x")


def test_budget_caps_output_and_reports_omitted(store):
    for i in range(6):
        active_lesson(store, rule=f"Rule {i} " + "r" * 390, paths=["src/a.py"], base=10 * (i + 1), why="y" * 600)
    result = store.get_relevant_lessons(["src/a.py"], limit=20)
    size = sum(len(json.dumps(x, separators=(",", ":"), ensure_ascii=False)) for x in result["lessons"])
    assert size <= rules.SERVE_TOKEN_BUDGET * 4 and result["approx_tokens"] <= rules.SERVE_TOKEN_BUDGET
    assert 0 < len(result["lessons"]) < 6 and result["omitted"] == 6 - len(result["lessons"])
    assert result["matched"] == 6


def test_example_served_only_when_it_fits(store):
    example = {"bad": "retry(post)\n" * 40, "good": "post_once()\n" * 40}
    learn(store, obs(3, kind="revert", quote="", example=example), obs(4, example=example, reviewer="carol"))
    assert store.get_relevant_lessons(["src/api/a.py"])["lessons"][0]["example"]["good"].startswith("post_once()")
    for i in range(4):
        active_lesson(store, rule=f"Filler {i} " + "f" * 390, paths=["src/api/a.py"], base=100 + 10 * i, why="y" * 600)
    crowded = store.get_relevant_lessons(["src/api/a.py"], limit=20)
    assert crowded["lessons"][0]["id"] == "L1" and "example" not in crowded["lessons"][0]
    assert crowded["omitted"] > 0 and all("example" not in x for x in crowded["lessons"])


def test_get_lessons_is_the_full_listing_with_flags(store):
    active_lesson(store)
    learn(store, obs(20, rule="Only candidate"))
    rec(store, repeat_obs(30))
    listing = store.get_lessons()
    assert listing["pending_checks"] == 1
    by_id = {x["id"]: x for x in listing["lessons"]}
    assert by_id["L1"]["pending"][0]["type"] == "repeat" and by_id["L2"]["status"] == "candidate"
    assert {"status", "score", "scope", "kind", "pending"} <= set(by_id["L1"])


# -- LESSONS.md -------------------------------------------------------------------------
def test_lessons_md_sections_and_counts(store):
    active_lesson(store)
    learn(store, obs(20, rule="Only a candidate"))
    rec(store, obs(1, kind="review_comment"), obs(2, reviewer="bob"), repeat_obs(30))
    text = (store.dir / "LESSONS.md").read_text()
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Must not repeat", "## Rules", "## Candidates (not yet rules)", "## Pending checks",
                        "## Archived"]
    assert "### L2" in text.split("## Candidates")[1] and "not yet a rule" in text
    assert "1 pending check(s)" in text and "Discarded observations: 1. Weak review comments (counted only): 1." in text
    assert "team rule, score" in text


# -- export -------------------------------------------------------------------------------
def run_cli(*args):
    with patch.object(sys, "argv", ["pitfall-mcp", "lessons", "--repo", REPO, *args]):
        live.main()


def test_export_creates_replaces_only_the_block_and_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_PR_CONTEXT_HOME", str(tmp_path / "home"))
    store = LessonStore(REPO)
    active_lesson(store, rule="Never retry <b>POST</b> [x](javascript:1) `calls`")
    target = tmp_path / "nested" / "AGENTS.md"
    run_cli("--export", str(target))
    first = target.read_text()
    assert "Exported 1 lesson(s)" in capsys.readouterr().out
    assert first.startswith("<!-- pitfall:lessons:start -->") and first.rstrip().endswith("<!-- pitfall:lessons:end -->")
    assert "advisory" in first.lower() and "derived from past PR reviews" in first
    assert "<b>" not in first and "&lt;b&gt;" in first and "\\[x\\]" in first and "\\`calls\\`" in first
    assert first.count("pitfall:lessons:start") == 1

    target.write_text("# My agent notes\n\nkeep me\n\n" + first.strip() + "\n\n## Tail\nkeep me too\n")
    learn(store, obs(9, rule="Second rule", kind="revert", quote="", paths=["docs/a.md"]))
    run_cli("--export", str(target))
    updated = target.read_text()
    assert updated.startswith("# My agent notes\n\nkeep me\n\n") and updated.endswith("## Tail\nkeep me too\n")
    assert updated.count("pitfall:lessons:start") == 1 and "Second rule" in updated
    run_cli("--export", str(target))
    assert target.read_text() == updated and "(unchanged)" in capsys.readouterr().out.splitlines()[-1]


def test_export_appends_to_a_file_without_markers_and_orders_by_score(tmp_path, store):
    learn(store, obs(3, rule="Low", reviewer="alice"), obs(4, rule="Low", reviewer="alice"))
    learn(store, obs(5, rule="High", kind="revert", quote="", reviewer="alice"),
          obs(5, rule="High", kind="revert", quote="", reviewer="carol"))
    learn(store, obs(7, rule="Candidate only"))
    target = tmp_path / "CLAUDE.md"
    target.write_text("existing\n")
    assert store.export(target)["exported"] == 2
    text = target.read_text()
    assert text.startswith("existing\n\n<!-- pitfall:lessons:start -->")
    assert text.index("High") < text.index("Low") and "Candidate only" not in text


def test_export_caps_at_thirty_and_handles_empty_and_broken_files(tmp_path, store):
    for i in range(32):
        learn(store, obs(10 + i, rule=f"Rule number {i}", kind="revert", quote=""))
    target = tmp_path / "out.md"
    assert store.export(target)["exported"] == rules.EXPORT_MAX_LESSONS
    empty = LessonStore("acme/empty", home=tmp_path / "other")
    assert empty.export(tmp_path / "empty.md")["exported"] == 0
    assert "_No active lessons yet._" in (tmp_path / "empty.md").read_text()
    broken = tmp_path / "broken.md"
    broken.write_text("<!-- pitfall:lessons:start -->\nno end\n")
    with pytest.raises(LessonError):
        store.export(broken)
    assert broken.read_text() == "<!-- pitfall:lessons:start -->\nno end\n"


def test_export_flag_works_on_the_old_command_name_and_reports_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_PR_CONTEXT_HOME", str(tmp_path / "home"))
    target = tmp_path / "x.md"
    with patch.object(sys, "argv", ["github-pr-context-mcp", "lessons", "--repo", REPO, "--export", str(target)]):
        live.main()
    assert target.exists()
    target.write_text("<!-- pitfall:lessons:start -->")
    with patch.object(sys, "argv", ["x", "lessons", "--repo", REPO, "--export", str(target)]), pytest.raises(SystemExit):
        live.main()


# -- state schema 2 and migration from v1 --------------------------------------------------
def v1_state():
    def ev(number, note):
        return {"pr": number, "url": f"{BASE}/pull/{number}", "date": "2024-01-03", "note": note}
    base = {"category": "reliability", "why": "because", "created_at": "2024-01-04T00:00:00Z"}
    return {"schema_version": 1, "repo": REPO, "phase": "ongoing", "backfill_cursor": None,
            "newest_processed": {"number": 57, "created_at": "2024-03-01T00:00:00Z", "url": f"{BASE}/pull/57"},
            "processed_count": 1, "processed_numbers": [57], "next_lesson_id": 4, "next_alert_id": 2,
            "backfill_completed_at": None, "updated_at": None,
            "lessons": [
                {**base, "id": "L1", "rule": "Hardened rule", "evidence": [ev(3, "first"), ev(57, "again")],
                 "repeat_prs": [57], "repeat_count": 1, "hardened": True, "status": "active"},
                {**base, "id": "L2", "rule": "Single evidence rule", "evidence": [ev(4, "only")], "repeat_prs": [],
                 "repeat_count": 0, "hardened": False, "status": "active"},
                {**base, "id": "L3", "rule": "Gone", "evidence": [ev(5, "x")], "repeat_prs": [], "repeat_count": 0,
                 "hardened": False, "status": "removed", "removed_reason": "stale"}],
            "alerts": [{"id": "A1", "lesson_id": "L1", "pr": 57, "url": f"{BASE}/pull/57", "date": "2024-03-01",
                        "note": "again", "repeat_count": 1, "acknowledged": False, "created_at": "2024-03-01T00:00:00Z",
                        "message": "This type of mistake happened again"}]}


def test_v1_state_migrates_and_levels_are_recomputed(store):
    store.dir.mkdir(parents=True)
    store.state_path.write_text(json.dumps(v1_state()))
    listing = {x["id"]: x for x in store.get_lessons()["lessons"]}
    assert set(listing) == {"L1", "L2"}  # the removed lesson stays removed
    assert listing["L1"]["status"] == "hardened" and listing["L1"]["repeat_count"] == 1
    assert [e["kind"] for e in listing["L1"]["evidence"]] == ["change_request_fixed"] * 2
    assert listing["L1"]["evidence"][0]["quote"] == "first" and listing["L1"]["evidence"][1]["repeat"] is True
    assert listing["L2"]["status"] == "candidate"
    alert = store.list_alerts()["alerts"][0]
    assert alert["type"] == "repeat" and alert["lesson_id"] == "L1"
    assert json.loads(store.state_path.read_text())["schema_version"] == 1  # reads never rewrite
    store.acknowledge_alert("A1")
    state = json.loads(store.state_path.read_text())
    assert state["schema_version"] == 2 and state["pending"] == [] and state["stats"]["discarded"] == 0
    assert next(x for x in state["lessons"] if x["id"] == "L3")["removed"] is True
    assert "## Candidates (not yet rules)" in (store.dir / "LESSONS.md").read_text()
    assert store.record([], [{"rule": "x", "why": "y", "evidence": []}], [])["rejected"]


def test_unknown_schema_is_refused(store):
    store.dir.mkdir(parents=True)
    store.state_path.write_text(json.dumps({**v1_state(), "schema_version": 3}))
    with pytest.raises(LessonError):
        store.status()


# -- legacy params ------------------------------------------------------------------------------
def test_legacy_new_lessons_and_repeats_become_observations(store):
    legacy = {"rule": RULE, "category": "Reliability", "why": "Duplicate charges.",
              "evidence": [{"pr": n, "url": f"{BASE}/pull/{n}", "date": "2024-01-03", "note": ""} for n in (3, 4)]}
    result = store.record([], [legacy], [], True)
    assert len(result["pending_created"]) == 1 and result["discarded"] == []  # empty quote is allowed for legacy
    decide(store)
    assert lesson_of(store)["status"] == "active" and lesson_of(store)["kind"] == "preference"
    store.record([], [], [{"lesson_id": "L1", "pr": 9, "url": f"{BASE}/pull/9", "date": "2024-02-01", "note": "again"}])
    assert decide(store)["alerts"][0]["pr"] == 9


# -- MCP tools ------------------------------------------------------------------------------------
def call(name, **args):
    result = asyncio.run(live.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_new_tools_are_registered_and_work_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_PR_CONTEXT_HOME", str(tmp_path))
    names = {t.name for t in asyncio.run(live.mcp.list_tools())}
    assert {"list_pending_checks", "submit_checks", "get_relevant_lessons", "resolve_conflict", "list_candidates",
            "approve_lesson", "record_learning", "get_lessons"} <= names
    assert "export" not in " ".join(names)  # the export is CLI only
    recorded = call("record_learning", repo=REPO, processed_prs=[pr(3)], observations=[obs(3), obs(4, reviewer="carol")])
    assert len(recorded["pending_created"]) == 1 and recorded["discarded"] == []
    checks = call("list_pending_checks", repo=REPO)["checks"]
    call("submit_checks", repo=REPO, decisions=[{"check_id": checks[0]["check_id"], "verdict": "approve", "reason": "ok"}])
    served = call("get_relevant_lessons", repo=REPO, paths=["src/api/other.py"])
    assert served["lessons"][0]["id"] == "L1" and served["omitted"] == 0
    assert call("list_candidates", repo=REPO)["candidates"] == []
    call("record_learning", repo=REPO, processed_prs=[], observations=[obs(9, rule="Cand rule")])
    cid = call("list_pending_checks", repo=REPO)["checks"][0]["check_id"]
    call("submit_checks", repo=REPO, decisions=[{"check_id": cid, "verdict": "approve"}])
    assert call("approve_lesson", repo=REPO, lesson_id="L2")["status"] == "active"
    assert call("approve_lesson", repo=REPO, lesson_id="L9")["ok"] is False
    assert call("resolve_conflict", repo=REPO, alert_id="A1", decision="keep")["ok"] is False
    assert call("get_relevant_lessons", repo=REPO, paths=[])["lessons"] == []  # scoped lessons need a matching path
    assert call("learning_status", repo=REPO)["lessons_active"] == 2


def test_prompts_describe_the_label_check_apply_split(tmp_path):
    learn = asyncio.run(live.mcp.get_prompt("learn_from_prs", {"repo": REPO})).messages[0].content.text
    for word in ("observations", "submit_checks", "list_pending_checks", "sub-agent", "check_pending_rules", "quote"):
        assert word in learn
    check = asyncio.run(live.mcp.get_prompt("check_pending_rules", {"repo": REPO})).messages[0].content.text
    assert "list_pending_checks" in check and "not the learner's reasoning" in check and json.dumps({"repo": REPO}) in check
    before = asyncio.run(live.mcp.get_prompt("check_lessons_before_change", {"repo": REPO, "task": "t"}))
    assert "get_relevant_lessons" in before.messages[0].content.text


# -- concurrency ------------------------------------------------------------------------------------
def test_concurrent_record_and_submit_do_not_lose_updates(tmp_path):
    errors = []

    def record(base):
        try:
            for i in range(8):
                number = base + i
                LessonStore(REPO, home=tmp_path).record(
                    [pr(number)], [], [], False, [obs(number, rule=f"rule {number}", kind="revert", quote="")])
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def approve(ids):
        try:
            LessonStore(REPO, home=tmp_path).submit_checks(
                [{"check_id": cid, "verdict": "approve"} for cid in ids])
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(base,)) for base in (100, 200, 300)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    store = LessonStore(REPO, home=tmp_path)
    ids = [c["check_id"] for c in store.list_pending_checks()["checks"]]
    assert not errors and len(ids) == 24 and len(set(ids)) == 24
    threads = [threading.Thread(target=approve, args=(ids[i::3],)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = store.load()
    assert not errors and state["pending"] == [] and len(state["lessons"]) == 24
    assert len({x["id"] for x in state["lessons"]}) == 24 and state["processed_count"] == 24
