"""Mistake families: taxonomy, principle/specific rules, family repeats, stats, serving by kind, merges, migration."""

import asyncio
import json
import sys
from unittest.mock import patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from entrypoints.local import live, rules
from entrypoints.local.lessons import LessonError, LessonStore

REPO = "acme/widgets"
BASE = f"https://github.com/{REPO}"
TIMEOUT = "missing-timeout"
HTTP_RULE = "Pass timeout= to requests.get in the API client"
DB_RULE = "Pass a statement timeout to cursor.execute in the db layer"
PRINCIPLE = "Every outbound network or database call needs a timeout"


def pr(number, created_at="2024-01-10T10:00:00Z"):
    return {"number": number, "url": f"{BASE}/pull/{number}", "created_at": created_at, "merged": True}


def obs(number=3, **extra):
    item = {"pr": number, "url": f"{BASE}/pull/{number}#discussion_r1", "date": "2024-01-03",
            "kind": "change_request_fixed", "reviewer": "alice", "author": "bob",
            "paths": ["src/api/client.py"], "rule": HTTP_RULE, "why": "A hung request blocked the worker.",
            "quote": "Please add a timeout to this call."}
    item.update(extra)
    return item


@pytest.fixture
def store(tmp_path):
    return LessonStore(REPO, home=tmp_path)


def rec(store, *observations, prs=(), complete=False):
    return store.record(list(prs), [], [], complete, list(observations))


def decide(store, verdict="approve", only=None, **extra):
    checks = store.list_pending_checks()["checks"]
    return store.submit_checks([{"check_id": c["check_id"], "verdict": verdict, "reason": "checked", **extra}
                                for c in checks if only is None or c["type"] == only])


def learn(store, *observations, **kwargs):
    rec(store, *observations, **kwargs)
    return decide(store)


def lesson_of(store, lesson_id="L1"):
    return next(x for x in store.get_lessons()["lessons"] if x["id"] == lesson_id)


def active_lesson(store, rule=HTTP_RULE, base=3, family=TIMEOUT, principle=PRINCIPLE, **extra):
    """Active lesson: strong evidence in two distinct PRs from two reviewers."""
    learn(store, obs(base, rule=rule, family=family, principle=principle, **extra),
          obs(base + 1, rule=rule, family=family, principle=principle, reviewer="carol", **extra))


def timeout_family(store):
    """L1 (HTTP, change requests) and L2 (DB, reverts: the stronger one), both missing-timeout; L3 resource-leak."""
    active_lesson(store, rule=HTTP_RULE, base=3, paths=["src/api/client.py"])
    active_lesson(store, rule=DB_RULE, base=10, paths=["src/db/query.py"], kind="revert", quote="")
    active_lesson(store, rule="Close file handles in the importer", base=20, family="resource-leak",
                  principle="Every opened handle must be closed", paths=["src/io/importer.py"])


def family_repeat(number, rule="Add a timeout to the redis calls", family=TIMEOUT, **extra):
    item = {"rule": rule, "family": family, "family_repeat": True, "why": "", "paths": ["src/cache/redis.py"],
            "date": "2024-02-01"}
    item.update(extra)
    return obs(number, **item)


def served(store, paths, **kwargs):
    return [x["id"] for x in store.get_relevant_lessons(paths, **kwargs)["lessons"]]


def call(name, **args):
    result = asyncio.run(live.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# -- taxonomy ----------------------------------------------------------------------
def test_taxonomy_is_small_documented_and_ends_with_other():
    names = list(rules.FAMILIES)
    assert 10 <= len(names) <= 20 and names[-1] == "other" and rules.OTHER_FAMILY == "other"
    assert {"missing-timeout", "blocking-io", "resource-leak", "error-handling", "concurrency", "input-validation",
            "security", "api-misuse", "performance", "data-integrity", "naming-and-style", "tests",
            "docs-and-changelog", "config-and-deps"} <= set(names)
    assert all(isinstance(text, str) and 10 < len(text) < 100 for text in rules.FAMILIES.values())
    assert rules.list_families()[0] == {"name": "missing-timeout", "description": rules.FAMILIES["missing-timeout"]}


@pytest.mark.parametrize("value,expected", [
    ("missing-timeout", "missing-timeout"), ("Missing_Timeout", "missing-timeout"), (" tests ", "tests"),
    ("missing timeout", "missing-timeout"), ("made-up", "other"), ("", "other"), (None, "other"), (5, "other"),
])
def test_unknown_family_becomes_other(value, expected):
    assert rules.normalise_family(value) == expected


def test_unknown_family_on_an_observation_is_stored_as_other(store):
    learn(store, obs(3, family="Made Up"), obs(4, family="Made Up", reviewer="carol"))
    assert lesson_of(store)["family"] == "other"
    with pytest.raises(LessonError):
        rec(store, obs(5, family=7))
    with pytest.raises(LessonError):
        rec(store, obs(5, family_repeat="yes"))
    with pytest.raises(LessonError):
        rec(store, obs(5, merge_with="L1"))


# -- principle and specific rule ---------------------------------------------------------
def test_principle_and_specific_rule_are_stored_and_shown_to_the_checker(store):
    rec(store, obs(3, principle=PRINCIPLE, family=TIMEOUT))
    check = store.list_pending_checks()["checks"][0]
    assert check["rule"] == HTTP_RULE and check["principle"] == PRINCIPLE and check["family"] == TIMEOUT
    assert '"why"' not in json.dumps(check)  # the learner's reasoning stays hidden
    decide(store)
    got = lesson_of(store)
    assert got["rule"] == HTTP_RULE and got["principle"] == PRINCIPLE and got["family"] == TIMEOUT


def test_without_a_principle_or_family_the_rule_is_the_principle_and_family_is_other(store):
    learn(store, obs(3))
    got = lesson_of(store)
    assert got["principle"] == got["rule"] == HTTP_RULE and got["family"] == "other"
    check_state = store.load()["lessons"][0]
    assert check_state["merged_into"] is None and check_state["merged_from"] == []


def test_checker_can_edit_principle_and_family_without_touching_the_rule(store):
    rec(store, obs(3, principle="Every call needs a timeout and a retry budget and a circuit breaker",
                   family="Made Up"))
    result = decide(store, "edit", principle=PRINCIPLE, family="Missing_Timeout")
    assert result["new_lessons"][0]["principle"] == PRINCIPLE and result["new_lessons"][0]["family"] == TIMEOUT
    got = lesson_of(store)
    assert got["rule"] == HTTP_RULE and got["principle"] == PRINCIPLE and got["family"] == TIMEOUT


def test_checker_can_edit_family_and_rule_together_and_an_empty_edit_is_an_error(store):
    rec(store, obs(3))
    cid = store.list_pending_checks()["checks"][0]["check_id"]
    assert "rule, principle or family" in store.submit_checks([{"check_id": cid, "verdict": "edit"}])["errors"][0]["reason"]
    store.submit_checks([{"check_id": cid, "verdict": "edit", "rule": "Add a timeout to HTTP calls", "family": "api-misuse"}])
    got = lesson_of(store)
    assert got["rule"] == "Add a timeout to HTTP calls" and got["family"] == "api-misuse"
    assert got["principle"] == "Add a timeout to HTTP calls"  # no principle was given: the rule stands in


def test_existing_lesson_can_be_reclassified_through_a_gated_rule_edit(store):
    active_lesson(store, family=None, principle=None)  # as migrated lessons: family other
    assert lesson_of(store)["family"] == "other"
    rec(store, obs(9, lesson_id="L1", reviewer="dave", family=TIMEOUT, principle=PRINCIPLE))
    check = next(c for c in store.list_pending_checks()["checks"] if c["type"] == "rule_edit")
    assert check["current_family"] == "other" and check["family"] == TIMEOUT and check["principle"] == PRINCIPLE
    assert lesson_of(store)["family"] == "other"  # nothing changes before the checker approves
    decide(store, only="rule_edit")
    assert lesson_of(store)["family"] == TIMEOUT and lesson_of(store)["principle"] == PRINCIPLE
    assert lesson_of(store)["rule"] == HTTP_RULE


# -- family-level repeats ------------------------------------------------------------------
def test_family_repeat_hardens_the_strongest_lesson_and_alerts_with_family_wording(store):
    timeout_family(store)
    rec(store, complete=True)
    result = rec(store, family_repeat(40))
    assert result["pending_created"][0]["type"] == "family_repeat" and result["alerts"] == []
    assert store.list_alerts()["alerts"] == []  # nothing before the checker approves
    check = store.list_pending_checks()["checks"][0]
    assert check["type"] == "family_repeat" and check["family"] == TIMEOUT and check["lesson_id"] is None
    assert check["family_description"] == rules.FAMILIES[TIMEOUT]
    assert [x["id"] for x in check["related_lessons"]] == ["L2", "L1"]  # strongest first, other families absent
    applied = decide(store)
    assert applied["repeats_applied"] == [{"lesson_id": "L2", "pr": 40, "repeat_count": 1, "family": TIMEOUT}]
    expected = (f"This kind of mistake ({rules.FAMILIES[TIMEOUT]}) happened again in PR #40 "
                "(related lesson L2) - you may want to look at it.")
    assert applied["tell_user"] == expected
    alert = store.list_alerts()["alerts"][0]
    assert alert["type"] == "repeat" and alert["lesson_id"] == "L2" and alert["family"] == TIMEOUT
    assert alert["pr"] == 40 and alert["message"] == expected
    assert lesson_of(store, "L2")["status"] == "hardened" and lesson_of(store, "L2")["repeat_count"] == 1
    assert lesson_of(store, "L1")["status"] == "active" and lesson_of(store, "L3")["status"] == "active"


def test_family_repeat_can_name_the_related_lesson(store):
    timeout_family(store)
    rec(store, complete=True)
    rec(store, family_repeat(40))
    bad = decide(store, lesson_id="L3")  # resource-leak: not in this family
    assert "not an active lesson in family" in bad["errors"][0]["reason"] and store.list_pending_checks()["count"] == 1
    assert decide(store, lesson_id="L1")["alerts"][0]["lesson_id"] == "L1"
    assert lesson_of(store, "L1")["status"] == "hardened" and lesson_of(store, "L2")["status"] == "active"


def test_family_repeat_is_quiet_while_backfilling(store):
    timeout_family(store)
    rec(store, family_repeat(40))
    result = decide(store)
    assert result["alerts"] == [] and result["tell_user"] == "" and store.list_alerts()["alerts"] == []
    assert result["historical_repeats"] == [{"lesson_id": "L2", "pr": 40, "repeat_count": 1, "family": TIMEOUT}]
    assert lesson_of(store, "L2")["status"] == "hardened"
    # The phase when the claim was recorded decides, not when it was checked.
    rec(store, family_repeat(41), complete=True)
    assert decide(store)["alerts"] == []
    rec(store, family_repeat(42))
    assert decide(store)["alerts"][0]["pr"] == 42


def test_family_repeat_is_idempotent_per_family_and_pr(store):
    timeout_family(store)
    rec(store, complete=True)
    first = rec(store, family_repeat(40), family_repeat(40, rule="Add a timeout to the kafka producer"))
    assert len(first["pending_created"]) == 1  # one claim per family and PR
    decide(store)
    again = rec(store, family_repeat(40))
    assert again["pending_created"] == [] and again["evidence_skipped"][0]["reason"] == "claim already decided"
    assert decide(store)["alerts"] == [] and len(store.list_alerts()["alerts"]) == 1
    assert lesson_of(store, "L2")["repeat_count"] == 1
    # A different PR is a new claim.
    assert rec(store, family_repeat(41))["pending_created"]


def test_rejected_family_repeat_is_not_requeued_and_changes_nothing(store):
    timeout_family(store)
    rec(store, complete=True)
    rec(store, family_repeat(40))
    decide(store, "reject", reason="only looks similar")
    assert lesson_of(store, "L2")["repeat_count"] == 0 and store.list_alerts()["alerts"] == []
    again = rec(store, family_repeat(40))
    assert again["pending_created"] == [] and again["discarded"]


def test_family_repeat_needs_an_active_lesson_in_a_real_family(store):
    timeout_family(store)
    learn(store, obs(30, rule="A lone candidate", family="security", principle="x"))
    assert "no active lesson in family" in rec(store, family_repeat(40, family="security"))["discarded"][0]["reason"]
    assert "specific family" in rec(store, family_repeat(41, family="nonsense"))["discarded"][0]["reason"]
    assert rec(store, family_repeat(42, why="because", family_repeat=False))["pending_created"][0]["type"] == "new_lesson"  # no flag, has a why
    assert store.status()["pending_checks"] == 1


def test_family_repeat_without_flag_and_why_is_still_a_family_repeat(store):
    timeout_family(store)
    item = family_repeat(40)
    del item["family_repeat"]
    assert rec(store, item)["pending_created"][0]["type"] == "family_repeat"


def test_family_repeat_skips_a_lesson_the_pr_is_already_evidence_for(store):
    active_lesson(store)  # L1: PRs 3 and 4
    rec(store, complete=True)
    rec(store, family_repeat(3))
    result = decide(store)
    assert result["repeats_applied"] == [] and result["repeats_skipped"] == [{"family": TIMEOUT, "pr": 3}]
    assert result["alerts"] == [] and lesson_of(store)["status"] == "active"


def test_lesson_repeat_keeps_its_wording(store):
    active_lesson(store)
    rec(store, complete=True)
    rec(store, obs(9, lesson_id="L1"))
    result = decide(store)
    assert result["tell_user"].startswith("This type of mistake happened again (lesson L1, PR #9")
    assert "family" not in result["alerts"][0]


# -- family stats -----------------------------------------------------------------------------
def test_family_stats_in_lessons_status_and_markdown(store):
    timeout_family(store)
    rec(store, complete=True)
    rec(store, family_repeat(40, date="2024-02-01"))
    decide(store)
    rec(store, family_repeat(41, date="2024-03-05"))
    decide(store)
    stats = store.get_lessons()["families"]
    assert stats[0]["family"] == TIMEOUT and stats[0]["lessons"] == 2 and stats[0]["repeats"] == 2
    assert stats[0]["last_repeat"] == "2024-03-05"
    assert stats[1] == {"family": "resource-leak", "description": rules.FAMILIES["resource-leak"], "lessons": 1,
                        "hardened": 0, "repeats": 0, "last_repeat": None}
    assert [row["family"] for row in store.status()["families"]] == [TIMEOUT, "resource-leak"]
    text = (store.dir / "LESSONS.md").read_text()
    table = text.split("## Mistake families")[1].split("## Must not repeat")[0]
    assert "| missing-timeout | 2 | 1 | 2 | 2024-03-05 |" in table and "| resource-leak | 1 | 0 | 0 | - |" in table
    assert "- Family: missing-timeout" in text and f"- Principle: {PRINCIPLE}" in text


def test_family_stats_leave_out_empty_families_and_removed_lessons(store):
    assert store.get_lessons()["families"] == []
    active_lesson(store)
    store.remove_lesson("L1", "stale")
    assert store.get_lessons()["families"] == []


# -- serving by kind ----------------------------------------------------------------------------
def test_family_lessons_from_other_folders_are_served_only_when_the_family_is_requested(store):
    timeout_family(store)
    assert served(store, ["src/api/new.py"]) == ["L1"]
    assert served(store, ["src/api/new.py"], families=[]) == ["L1"]
    assert served(store, ["src/api/new.py"], families=[TIMEOUT]) == ["L1", "L2"]
    assert served(store, ["src/web/page.py"]) == []
    assert served(store, ["src/web/page.py"], families=[TIMEOUT]) == ["L2", "L1"]
    assert sorted(served(store, ["src/web/page.py"], families=["Missing_Timeout", "resource-leak"])) == ["L1", "L2", "L3"]


def test_path_matches_come_before_family_matches_then_score_and_hardened_leads(store):
    timeout_family(store)
    got = store.get_relevant_lessons(["src/api/new.py"], families=[TIMEOUT])["lessons"]
    scores = {x["id"]: x["score"] for x in got}
    assert [x["id"] for x in got] == ["L1", "L2"] and scores["L2"] > scores["L1"]  # folder match beats a higher score
    rec(store, family_repeat(40), complete=True)
    decide(store)  # hardens L2, the strongest in the family
    got = store.get_relevant_lessons(["src/api/new.py"], families=[TIMEOUT])["lessons"]
    assert [(x["id"], x["status"]) for x in got] == [("L2", "hardened"), ("L1", "active")]  # hardened still first


def test_matched_by_reports_path_family_and_global(store):
    timeout_family(store)
    active_lesson(store, rule="Pin the lockfile", base=40, family="config-and-deps", principle="Pin dependencies",
                  paths=["a/x.py", "b/y.py", "c/z.py"])  # L4: global
    got = {x["id"]: x for x in store.get_relevant_lessons(["src/api/new.py"], families=[TIMEOUT])["lessons"]}
    assert {i: x["matched_by"] for i, x in got.items()} == {"L1": "path", "L2": "family", "L4": "global"}
    # Both folder and family match: the folder reason wins. Without the family it is still path.
    got = {x["id"]: x["matched_by"] for x in store.get_relevant_lessons(["src/api/new.py"])["lessons"]}
    assert got == {"L1": "path", "L4": "global"}
    result = store.get_relevant_lessons(["src/web/p.py"], families=["config-and-deps", TIMEOUT])
    assert [(x["id"], x["matched_by"]) for x in result["lessons"]] == [("L4", "global"), ("L2", "family"), ("L1", "family")]
    assert result["families"] == [TIMEOUT, "config-and-deps"]  # taxonomy order


def test_family_serving_never_serves_candidates_archived_or_merged_lessons(store):
    timeout_family(store)
    learn(store, obs(60, rule="Only a candidate", family=TIMEOUT, principle="x", paths=["src/zzz/a.py"]))
    assert served(store, ["src/web/p.py"], families=[TIMEOUT]) == ["L2", "L1"]
    rec(store, prs=[pr(900, "2026-01-01T00:00:00Z")])  # everything is now over a year old
    assert served(store, ["src/web/p.py"], families=[TIMEOUT]) == []


def test_unknown_and_invalid_families_arguments(store):
    timeout_family(store)
    result = store.get_relevant_lessons(["src/api/new.py"], families=["bogus", "Made Up", "bogus"])
    assert [x["id"] for x in result["lessons"]] == ["L1"] and result["unknown_families"] == ["bogus", "made-up"]
    assert result["families"] == []
    assert "unknown_families" not in store.get_relevant_lessons(["src/api/new.py"])
    for bad in ("missing-timeout", [1], [None], list(rules.FAMILIES) * 2):
        with pytest.raises(LessonError):
            store.get_relevant_lessons(["src/api/new.py"], families=bad)


def test_compact_form_carries_principle_and_family_and_the_specific_rule_when_it_fits(store):
    timeout_family(store)
    entry = store.get_relevant_lessons(["src/api/new.py"], families=[TIMEOUT])["lessons"][0]
    assert entry["principle"] == PRINCIPLE and entry["rule"] == HTTP_RULE and entry["family"] == TIMEOUT
    assert set(entry) == {"id", "principle", "rule", "family", "why", "status", "kind", "score", "link", "matched_by"}


def test_budget_prefers_principles_over_specific_rules(store, tmp_path):
    for i in range(8):
        active_lesson(store, rule=f"Specific {i} " + "s" * 380, principle=f"General {i} " + "g" * 90,
                      base=10 * (i + 1), why="y" * 600, paths=["src/a.py"])
    result = store.get_relevant_lessons(["src/a.py"], limit=20)
    size = sum(len(json.dumps(x, separators=(",", ":"), ensure_ascii=False)) for x in result["lessons"])
    assert size <= rules.SERVE_TOKEN_BUDGET * 4 and result["approx_tokens"] <= rules.SERVE_TOKEN_BUDGET
    assert result["lessons"] and all(x["principle"].startswith("General") for x in result["lessons"])
    with_rule = [x["id"] for x in result["lessons"] if "rule" in x]
    assert len(with_rule) < len(result["lessons"])  # the budget was tight: some entries carry only the principle
    ids = [x["id"] for x in result["lessons"]]
    assert with_rule == ids[:len(with_rule)]  # specifics go to the best-ranked entries first
    only = LessonStore(REPO, home=tmp_path / "solo")
    active_lesson(only, rule="Specific " + "s" * 300, principle="General", base=3, paths=["src/a.py"])
    assert only.get_relevant_lessons(["src/a.py"])["lessons"][0]["rule"].startswith("Specific")


# -- merging similar lessons ----------------------------------------------------------------------
MERGED = "Every outbound call needs an explicit timeout"


def test_merge_is_gated_and_folds_evidence_into_the_strongest_lesson(store):
    timeout_family(store)
    queued = store.propose_merge(["L1", "L2"], MERGED, "same mistake: a call without a timeout")
    assert queued["check_id"] == "C" + str(queued["check_id"][1:])
    assert store.propose_merge(["L2", "L1"], MERGED, "again")["check_id"] == queued["check_id"]  # one pending merge
    assert store.list_pending_checks()["count"] == 1
    check = store.list_pending_checks()["checks"][0]
    assert check["type"] == "merge" and check["principle"] == MERGED and check["family"] == TIMEOUT
    assert [x["id"] for x in check["lessons"]] == ["L1", "L2"] and check["lessons"][0]["evidence"][0]["quote"]
    assert [p["type"] for p in lesson_of(store, "L1")["pending"]] == ["merge"]
    assert served(store, ["src/db/q.py"]) == ["L2"] and lesson_of(store, "L1")["status"] == "active"  # nothing yet

    result = decide(store)
    assert result["merges"] == [{"kept": "L2", "merged": ["L1"], "principle": MERGED}]
    listing = store.get_lessons()
    assert [x["id"] for x in listing["lessons"]] == ["L2", "L3"] and listing["merged"] == [{"id": "L1", "into": "L2"}]
    kept = lesson_of(store, "L2")
    assert kept["principle"] == MERGED and kept["rule"] == DB_RULE and kept["merged_from"] == ["L1"]
    assert sorted(e["pr"] for e in kept["evidence"]) == [3, 4, 10, 11]
    state = store.load()
    assert state["lessons"][0]["merged_into"] == "L2" and state["lessons"][0]["removed"] is False
    assert served(store, ["src/api/new.py"], families=[TIMEOUT]) == ["L2"]  # L1 is never served again
    assert served(store, ["src/api/client.py"]) == ["L2"]  # its folders now belong to L2
    assert "### L1" not in (store.dir / "LESSONS.md").read_text()
    assert store.status()["lessons_active"] == 2


def test_merge_keeps_repeats_and_moves_pending_checks_to_the_kept_lesson(store):
    timeout_family(store)
    rec(store, complete=True)
    rec(store, obs(9, lesson_id="L1", reviewer="dave"))  # repeat claim on L1
    decide(store)
    assert lesson_of(store, "L1")["status"] == "hardened"
    rec(store, obs(12, lesson_id="L2", reviewer="erin"))  # pending claim on L2
    store.propose_merge(["L1", "L2"], MERGED, "same")
    assert decide(store, only="merge")["merges"] == [{"kept": "L1", "merged": ["L2"], "principle": MERGED}]
    kept = lesson_of(store, "L1")  # hardened beats active: the hardened lesson is the strongest and keeps its id
    assert kept["status"] == "hardened" and kept["repeat_count"] == 1 and kept["merged_from"] == ["L2"]
    assert sorted(e["pr"] for e in kept["evidence"] if not e.get("repeat")) == [3, 4, 10, 11]
    pending = store.list_pending_checks()["checks"][0]
    assert pending["type"] == "repeat" and pending["lesson_id"] == "L1"
    # Observations that still name the merged lesson land on the lesson that absorbed it.
    result = rec(store, obs(14, lesson_id="L2", reviewer="frank"))
    assert result["pending_created"][0]["lesson_id"] == "L1"
    assert {a["lesson_id"] for a in decide(store)["alerts"]} == {"L1"}


def test_merge_can_be_proposed_by_an_observation(store):
    timeout_family(store)
    result = rec(store, obs(3, lesson_id="L1", merge_with=["L2"], principle=MERGED, why="same mistake"))
    assert result["merges_refused"] == [] and result["evidence_skipped"]  # PR 3 is already counted for L1
    assert [c["type"] for c in store.list_pending_checks()["checks"]] == ["merge"]
    decide(store)
    assert lesson_of(store, "L2")["principle"] == MERGED
    # An observation proposing a merge without the merged principle is refused, the rest still works.
    refused = rec(store, obs(20, lesson_id="L3", merge_with=["L2"]))
    assert "principle" in refused["merges_refused"][0]["reason"]


def test_merge_rejected_changes_nothing_and_is_not_requeued(store):
    timeout_family(store)
    store.propose_merge(["L1", "L2"], MERGED, "maybe")
    decide(store, "reject", reason="related, not the same")
    assert [x["id"] for x in store.get_lessons()["lessons"]] == ["L2", "L1", "L3"]
    assert served(store, ["src/api/client.py", "src/db/q.py"]) == ["L2", "L1"]
    assert lesson_of(store, "L2")["principle"] == PRINCIPLE and store.get_lessons()["merged"] == []
    with pytest.raises(LessonError, match="already rejected"):
        store.propose_merge(["L1", "L2"], MERGED, "again")
    result = rec(store, obs(3, lesson_id="L1", merge_with=["L2"], principle=MERGED))
    assert "already rejected" in result["merges_refused"][0]["reason"] and store.list_pending_checks()["count"] == 0


def test_merge_across_families_is_refused(store):
    timeout_family(store)
    with pytest.raises(LessonError, match="different families"):
        store.propose_merge(["L1", "L3"], MERGED, "no")
    result = rec(store, obs(3, lesson_id="L1", merge_with=["L3"], principle=MERGED))
    assert "different families" in result["merges_refused"][0]["reason"] and store.list_pending_checks()["count"] == 0
    # Also at approval: a family changed after the proposal makes the merge refuse and stay pending.
    store.propose_merge(["L1", "L2"], MERGED, "same")
    state = store.load()
    state["lessons"][1]["family"] = "security"
    store._save(state)
    errors = decide(store)["errors"]
    assert "different families" in errors[0]["reason"] and store.list_pending_checks()["count"] == 1
    assert store.get_lessons()["merged"] == []
    decide(store, "reject", reason="families differ")
    assert store.list_pending_checks()["count"] == 0


def test_merge_input_validation(store):
    timeout_family(store)
    for ids in (["L1"], ["L1", "L1"], [], "L1", ["L1", "L9"], ["L1", "../x"], [f"L{i}" for i in range(1, 13)]):
        with pytest.raises(LessonError):
            store.propose_merge(ids, MERGED, "x")
    with pytest.raises(LessonError):
        store.propose_merge(["L1", "L2"], "", "x")
    with pytest.raises(LessonError):
        store.propose_merge(["L1", "L2"], MERGED, "")
    store.remove_lesson("L2", "stale")
    with pytest.raises(LessonError, match="unknown or removed"):
        store.propose_merge(["L1", "L2"], MERGED, "x")
    assert not [c for c in store.list_pending_checks()["checks"] if c["type"] == "merge"]


def test_checker_can_edit_the_merged_principle_only(store):
    timeout_family(store)
    store.propose_merge(["L1", "L2"], "Every call needs a timeout, retries and a breaker", "same")
    cid = store.list_pending_checks()["checks"][0]["check_id"]
    assert store.submit_checks([{"check_id": cid, "verdict": "edit", "rule": "x"}])["errors"]
    assert store.list_pending_checks()["count"] == 1
    store.submit_checks([{"check_id": cid, "verdict": "edit", "principle": MERGED}])
    assert lesson_of(store, "L2")["principle"] == MERGED


def test_merged_lessons_cannot_be_approved_removed_or_merged_again(store):
    timeout_family(store)
    store.propose_merge(["L1", "L2"], MERGED, "same")
    decide(store)
    for action in (lambda: store.remove_lesson("L1", "x"), lambda: store.approve_lesson("L1"),
                   lambda: store.propose_merge(["L1", "L3"], MERGED, "x")):
        with pytest.raises(LessonError):
            action()
    assert store.remove_lesson("L2", "stale")["status"] == "removed"
    assert served(store, ["src/api/client.py"], families=[TIMEOUT]) == []


def test_new_lesson_with_the_text_of_a_merged_lesson_counts_for_the_survivor(store):
    timeout_family(store)
    store.propose_merge(["L1", "L2"], MERGED, "same")
    decide(store)
    result = rec(store, obs(50, reviewer="dave"))  # HTTP_RULE is L1's rule text
    assert result["pending_created"][0] == {"check_id": result["pending_created"][0]["check_id"], "type": "repeat",
                                            "lesson_id": "L2", "rule": HTTP_RULE}


# -- schema 3 migration ---------------------------------------------------------------------------------
def downgrade_to_v2(store):
    state = json.loads(store.state_path.read_text())
    for lesson in state["lessons"]:
        for key in ("principle", "family", "merged_into", "merged_from"):
            lesson.pop(key)
    for check in state["pending"]:
        for key in ("principle", "family"):
            check.pop(key, None)
    state["schema_version"] = 2
    store.state_path.write_text(json.dumps(state))


def test_schema_2_state_migrates_to_3_with_family_other_and_rule_as_principle(store):
    active_lesson(store)
    rec(store, obs(30, rule="Pending proposal", kind="revert", quote=""))
    downgrade_to_v2(store)
    listing = store.get_lessons()
    only = listing["lessons"][0]
    assert only["family"] == "other" and only["principle"] == only["rule"] == HTTP_RULE and only["status"] == "active"
    assert listing["families"][0]["family"] == "other"
    check = store.list_pending_checks()["checks"][0]
    assert check["principle"] == "Pending proposal" and check["family"] == "other"
    assert json.loads(store.state_path.read_text())["schema_version"] == 2  # reads never rewrite
    assert served(store, ["src/api/x.py"]) == ["L1"] and served(store, ["src/web/x.py"], families=["other"]) == ["L1"]
    decide(store)  # the first write migrates the file for good
    state = json.loads(store.state_path.read_text())
    assert state["schema_version"] == 3
    assert [(x["family"], x["principle"]) for x in state["lessons"]] == [("other", HTTP_RULE),
                                                                          ("other", "Pending proposal")]


def test_schema_1_state_still_migrates_to_the_current_schema(store):
    ev = {"pr": 3, "url": f"{BASE}/pull/3", "date": "2024-01-03", "note": "first"}
    store.dir.mkdir(parents=True)
    store.state_path.write_text(json.dumps({
        "schema_version": 1, "repo": REPO, "phase": "ongoing", "backfill_cursor": None, "newest_processed": None,
        "processed_count": 0, "processed_numbers": [], "next_lesson_id": 2, "next_alert_id": 1,
        "backfill_completed_at": None, "updated_at": None, "alerts": [],
        "lessons": [{"id": "L1", "rule": "Old rule", "category": "x", "why": "y", "status": "active",
                     "evidence": [ev, {**ev, "pr": 4}], "repeat_prs": []}]}))
    got = store.get_lessons()["lessons"][0]
    assert got["family"] == "other" and got["principle"] == "Old rule" and got["status"] == "active"
    rec(store, prs=[pr(5)])
    assert json.loads(store.state_path.read_text())["schema_version"] == 3


# -- export -----------------------------------------------------------------------------------------------
def test_export_groups_by_family_and_uses_the_principle(store, tmp_path):
    timeout_family(store)
    active_lesson(store, rule="Unclassified thing", base=60, family=None, principle=None)
    target = tmp_path / "AGENTS.md"
    assert store.export(target)["exported"] == 4
    text = target.read_text()
    assert PRINCIPLE in text and HTTP_RULE not in text and DB_RULE not in text  # principles, not specifics
    order = [text.index(f"**{name}**") for name in (TIMEOUT, "resource-leak", "other")]
    assert order == sorted(order)
    group = text.split(f"**{TIMEOUT}**")[1].split("**resource-leak**")[0]
    assert "**L1**" in group and "**L2**" in group and "**L3**" not in group
    assert rules.FAMILIES[TIMEOUT] in text and text.rstrip().endswith("<!-- pitfall:lessons:end -->")
    before = target.read_text()
    store.export(target)
    assert target.read_text() == before  # idempotent


# -- MCP surface --------------------------------------------------------------------------------------------
def test_new_tools_and_parameter_are_registered_and_work_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_PR_CONTEXT_HOME", str(tmp_path))
    tools = {t.name: t for t in asyncio.run(live.mcp.list_tools())}
    assert {"list_families", "propose_merge", "get_relevant_lessons", "learning_status"} <= set(tools)
    properties = tools["get_relevant_lessons"].inputSchema["properties"]
    assert "families" in properties and properties["families"]["default"] is None
    assert set(tools["propose_merge"].inputSchema["required"]) == {"repo", "lesson_ids", "principle", "reason"}
    assert tools["list_families"].inputSchema.get("required", []) == []

    listed = call("list_families")
    assert [f["name"] for f in listed["families"]] == list(rules.FAMILIES) and listed["default"] == "other"

    store = LessonStore(REPO)
    timeout_family(store)
    got = call("get_relevant_lessons", repo=REPO, paths=["src/web/p.py"], families=[TIMEOUT])
    assert [(x["id"], x["matched_by"]) for x in got["lessons"]] == [("L2", "family"), ("L1", "family")]
    assert call("get_relevant_lessons", repo=REPO, paths=["src/web/p.py"])["lessons"] == []
    with pytest.raises(ToolError):  # the schema itself refuses a non-list
        call("get_relevant_lessons", repo=REPO, paths=[], families="oops")
    assert call("get_relevant_lessons", repo=REPO, paths=[], families=["a"] * 40)["ok"] is False
    assert call("learning_status", repo=REPO)["families"][0]["family"] == TIMEOUT

    proposed = call("propose_merge", repo=REPO, lesson_ids=["L1", "L2"], principle=MERGED, reason="same mistake")
    assert proposed["check_id"].startswith("C")
    assert call("propose_merge", repo=REPO, lesson_ids=["L1", "L3"], principle=MERGED, reason="x")["ok"] is False
    check = call("list_pending_checks", repo=REPO)["checks"][0]
    call("submit_checks", repo=REPO, decisions=[{"check_id": check["check_id"], "verdict": "approve"}])
    assert [x["id"] for x in call("get_lessons", repo=REPO)["lessons"]] == ["L2", "L3"]

    recorded = call("record_learning", repo=REPO, processed_prs=[pr(70)], observations=[
        family_repeat(70, rule="Add a timeout to the s3 client")])
    assert recorded["pending_created"][0]["type"] == "family_repeat" and recorded["pending_created"][0]["family"] == TIMEOUT


def test_prompts_teach_families_principles_and_family_loading(tmp_path):
    async def prompt(name, args):
        return (await live.mcp.get_prompt(name, args)).messages[0].content.text

    learn = asyncio.run(prompt("learn_from_prs", {"repo": REPO}))
    for word in ("list_families", "principle", "family_repeat", "merge_with", "propose_merge", "same kind of mistake"):
        assert word in learn
    checker = asyncio.run(prompt("check_pending_rules", {"repo": REPO}))
    for word in ("principle", "must not be broader", "family_repeat", "merge", "same kind of mistake"):
        assert word in checker
    before = asyncio.run(prompt("check_lessons_before_change", {"repo": REPO, "task": "t"}))
    assert before.index("list_families") < before.index("get_relevant_lessons")
    assert "families" in before and "matched_by" in before


def test_cli_alerts_show_the_family(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_PR_CONTEXT_HOME", str(tmp_path))
    store = LessonStore(REPO)
    timeout_family(store)
    rec(store, complete=True)
    rec(store, family_repeat(40))
    decide(store)
    with patch.object(sys, "argv", ["x", "lessons", "--repo", REPO, "--alerts"]):
        live.main()
    assert "lesson L2 (missing-timeout) PR #40" in capsys.readouterr().out


def test_repeats_without_rule_text_are_accepted(tmp_path):
    """A family repeat, or a repeat naming a lesson, needs no rule text of its own."""
    from entrypoints.local.lessons import LessonStore
    repo = "demo-org/demo-repo"
    base = f"https://github.com/{repo}/pull/"
    store = LessonStore(repo, home=tmp_path)

    def obs(number, reviewer, path, **extra):
        item = {"pr": number, "url": f"{base}{number}", "date": f"2026-01-{number:02d}",
                "kind": "change_request_fixed", "reviewer": reviewer, "author": f"dev{number}",
                "paths": [path], "quote": "please add a timeout here"}
        item.update(extra)
        return item

    def approve():
        checks = store.list_pending_checks()["checks"]
        if checks:
            store.submit_checks([{"check_id": c["check_id"], "verdict": "approve", "reason": "ok"} for c in checks])

    def pr(number):
        return {"number": number, "url": f"{base}{number}", "created_at": f"2026-01-{number:02d}T00:00:00Z",
                "merged": True}

    lesson = {"rule": "Set a timeout on requests.get", "principle": "Every outbound network call needs a timeout",
              "family": "missing-timeout", "why": "hung workers"}
    store.record([pr(10)], observations=[obs(10, "alice", "src/client/http.py", **lesson)])
    approve()
    store.record([pr(11)], observations=[obs(11, "bob", "src/client/http.py", lesson_id="L1")])
    approve()
    store.record([], backfill_complete=True)
    store.record([pr(20)], observations=[obs(20, "carol", "src/db/pool.py", family="missing-timeout",
                                             family_repeat=True)])
    approve()
    alerts = store.list_alerts()["alerts"]
    assert len(alerts) == 1 and "#20" in alerts[0]["message"]
    assert store.get_lessons()["lessons"][0]["status"] == "hardened"
