"""CI failures as a second feedback source: kinds, discards, signatures, run URLs, matching, prompt and stats."""

import asyncio
import json

import pytest

from entrypoints.local import live, rules
from entrypoints.local.lessons import LessonError, LessonStore

REPO = "acme/widgets"
BASE = f"https://github.com/{REPO}"
RULE = "Pin the pytest-asyncio version in requirements-dev.txt"
PRINCIPLE = "Pin test tooling versions so CI does not drift"


def pr(number, created_at="2024-01-10T10:00:00Z"):
    return {"number": number, "url": f"{BASE}/pull/{number}", "created_at": created_at, "merged": True}


def ci(number=3, **extra):
    item = {"pr": number, "url": f"{BASE}/pull/{number}", "date": "2024-01-03", "kind": "ci_failure_fixed",
            "source": "ci", "workflow": "CI", "job": "tests", "paths": ["tests/test_x.py", "requirements-dev.txt"],
            "failure_signature": f"tests/test_x.py:{number}: test_y AssertionError",
            "run_url": f"{BASE}/actions/runs/{number}00/job/{number}01",
            "rule": RULE, "principle": PRINCIPLE, "family": "config-and-deps",
            "why": "An unpinned plugin broke the test run.",
            "quote": f"E   AssertionError: assert {number} == 4"}
    item.update(extra)
    return item


@pytest.fixture
def store(tmp_path):
    return LessonStore(REPO, home=tmp_path)


def rec(store, *observations):
    return store.record([], [], [], False, list(observations))


def decide(store, verdict="approve", **extra):
    checks = store.list_pending_checks()["checks"]
    return store.submit_checks([{"check_id": c["check_id"], "verdict": verdict, "reason": "checked", **extra}
                                for c in checks])


def call(name, **args):
    result = asyncio.run(live.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# -- kinds and weights ---------------------------------------------------------------
def test_ci_kinds_are_registered_and_fixed_weighs_like_a_follow_up_fix():
    assert set(rules.CI_KINDS) == {"ci_failure_fixed", "ci_flaky", "ci_failure"}
    assert set(rules.CI_KINDS) <= set(rules.OBSERVATION_KINDS)
    assert rules.KIND_RANK["ci_failure_fixed"] == rules.KIND_RANK["follow_up_fix"] == 2
    assert "ci_flaky" not in rules.KIND_RANK and "ci_failure" not in rules.KIND_RANK


def test_one_fixed_failure_is_a_candidate_with_follow_up_fix_score(store):
    rec(store, ci(3))
    decide(store)
    lesson = store.get_lessons()["lessons"][0]
    # 2 x one follow-up-fix-weight PR, halved as a preference (no reviewers, one PR): 2 * 0.5
    assert lesson["status"] == "candidate" and lesson["kind"] == "preference" and lesson["score"] == 1.0


def test_two_prs_with_the_same_signature_become_an_active_team_lesson(store):
    first = rec(store, ci(3))
    assert first["pending_created"][0]["type"] == "new_lesson"
    # The second PR differs in line numbers, hashes and tmp paths and in the rule text, but is the same failure.
    second = rec(store, ci(4, failure_signature="tests/test_x.py:99: test_y AssertionError",
                           rule="Do not float the pytest plugin versions"))
    assert second["pending_created"] == [] and len(store.list_pending_checks()["checks"]) == 1
    decide(store)
    lesson = store.get_lessons()["lessons"][0]
    assert [e["pr"] for e in lesson["evidence"]] == [3, 4]
    assert lesson["status"] == "active" and lesson["kind"] == "team"
    assert lesson["score"] == 4.0  # 2 x 2 PRs, no reviewers
    assert lesson["family"] == "config-and-deps" and lesson["principle"] == PRINCIPLE
    assert [x["id"] for x in store.get_relevant_lessons(["requirements-dev.txt"])["lessons"]] == ["L1"]


def test_a_known_signature_adds_evidence_to_the_existing_lesson(store):
    rec(store, ci(3))
    decide(store)
    result = rec(store, ci(4, rule="A completely different wording of the rule"))
    assert result["evidence_added"] == [{"lesson_id": "L1", "pr": 4}] and result["pending_created"] == []
    assert store.get_lessons()["lessons"][0]["status"] == "active"


def test_naming_the_lesson_id_also_matches(store):
    rec(store, ci(3))
    decide(store)
    result = rec(store, ci(4, lesson_id="L1", failure_signature="mypy: incompatible type"))
    assert result["evidence_added"] == [{"lesson_id": "L1", "pr": 4}]


def test_a_third_pr_on_an_active_lesson_is_a_checked_repeat(store):
    rec(store, ci(3))
    rec(store, ci(4))
    decide(store)
    assert rec(store, ci(5))["pending_created"][0]["type"] == "repeat"


# -- discards ---------------------------------------------------------------------------
def test_flaky_and_unfixed_failures_are_discarded_and_counted(store):
    result = rec(store, ci(3, kind="ci_flaky"), ci(4, kind="ci_failure"), ci(5, kind="ci_flaky", quote="", rule=""))
    assert [d["kind"] for d in result["discarded"]] == ["ci_flaky", "ci_failure", "ci_flaky"]
    assert "flaky" in result["discarded"][0]["reason"] and "weak" in result["discarded"][1]["reason"]
    status = store.status()
    assert status["ci"] == {"fixed": 0, "flaky": 2, "weak": 1}
    assert status["discarded"] == 0 and status["pending_checks"] == 0
    assert "CI failures: 0 fixed (evidence), 2 flaky (discarded), 1 without a fix (discarded)." in store.markdown()


def test_ci_stats_count_approved_fixes_and_stay_out_of_markdown_when_unused(store):
    assert store.status()["ci"] == {"fixed": 0, "flaky": 0, "weak": 0}
    assert "CI failures" not in store.markdown()
    rec(store, ci(3))
    assert store.status()["ci"]["fixed"] == 0  # pending until the checker approves
    decide(store)
    assert store.status()["ci"]["fixed"] == 1
    assert "CI failures: 1 fixed (evidence)" in store.markdown()


@pytest.mark.parametrize("change,reason", [
    ({"failure_signature": ""}, "failure_signature"),
    ({"failure_signature": "12345 0xdeadbeef"}, "failure_signature"),
    ({"run_url": ""}, "run_url"),
    ({"quote": ""}, "quote"),
    ({"url": "https://github.com/other/repo/pull/3"}, "url"),
])
def test_a_fixed_failure_needs_signature_run_link_and_log_line(store, change, reason):
    result = rec(store, ci(3, **change))
    assert len(result["discarded"]) == 1 and reason in result["discarded"][0]["reason"]
    assert store.status()["discarded"] == 1 and store.list_pending_checks()["count"] == 0


def test_reviewer_rules_do_not_apply_to_ci(store):
    # No reviewer or author needed; a bot or the PR author as "reviewer" does not discard a CI observation.
    assert rec(store, ci(3))["pending_created"]
    assert rec(store, ci(4, reviewer="github-actions[bot]", author="github-actions[bot]"))["discarded"] == []


def test_ci_kinds_need_source_ci_and_other_kinds_refuse_it(store):
    with pytest.raises(LessonError):
        rec(store, ci(3, source="human"))
    with pytest.raises(LessonError):
        rec(store, ci(3, kind="revert", source="ci", reviewer="alice", author="bob"))
    assert rec(store, {k: v for k, v in ci(3).items() if k != "source"})["pending_created"]
    with pytest.raises(LessonError):
        rec(store, ci(3, workflow=5))


# -- signature normalisation --------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [
    ("tests/test_x.py:12: test_y AssertionError", "tests/test_x.py:480: test_y AssertionError"),
    ("TypeError at 0x7f3a9c1b2d40 in handler", "typeerror at 0x55d0c8a0 in handler"),
    ("FileNotFoundError /tmp/pytest-of-runner/pytest-3/test_a0/data.json",
     "FileNotFoundError /tmp/pytest-of-runner/pytest-91/test_a0/data.json"),
    ("cannot import name Foo from build 9f8e7d6c5b4a", "cannot import name Foo from build 1a2b3c4d5e6f"),
    ("timeout after 31.2s waiting for 550e8400-e29b-41d4-a716-446655440000",
     "timeout after 59.9s waiting for 123e4567-e89b-12d3-a456-426614174000"),
    ("/home/runner/work/widgets/widgets/tests/test_x.py::test_y FAILED", "tests/test_x.py::test_y FAILED"),
    ("  mypy:   incompatible   type ", "MYPY: incompatible type"),
])
def test_the_same_failure_normalises_to_the_same_signature(a, b):
    assert rules.normalise_failure_signature(a) == rules.normalise_failure_signature(b) != ""


def test_different_failures_keep_different_signatures():
    assert (rules.normalise_failure_signature("tests/test_x.py::test_y AssertionError")
            != rules.normalise_failure_signature("tests/test_x.py::test_z AssertionError"))
    assert (rules.normalise_failure_signature("mypy: incompatible type")
            != rules.normalise_failure_signature("mypy: missing return"))


def test_signature_is_capped_and_empty_without_letters():
    assert len(rules.normalise_failure_signature("x" * 500)) == rules.MAX_SIGNATURE
    assert rules.normalise_failure_signature("123 0xff") == "" and rules.normalise_failure_signature(None) == ""
    assert rules.normalise_failure_signature("tests/test_x.py::test_y AssertionError") == (
        "tests/test_x.py::test_y assertionerror")


def test_signature_is_stored_normalised(store):
    rec(store, ci(3, failure_signature="Tests/test_x.py:77: test_y AssertionError at 0xABCDEF12"))
    decide(store)
    evidence = store.get_lessons()["lessons"][0]["evidence"][0]
    assert evidence["failure_signature"] == "tests/test_x.py:N: test_y assertionerror at <hex>"
    assert evidence["source"] == "ci" and evidence["workflow"] == "CI" and evidence["job"] == "tests"


# -- run_url ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("run_url", [
    f"{BASE}/actions/runs/123", f"{BASE}/actions/runs/123/job/456", f"{BASE}/actions/runs/123/attempts/2".replace(
        "/attempts/2", ""), f"{BASE}/actions/runs/123?pr=3", f"https://github.com/ACME/Widgets/actions/runs/9",
])
def test_run_url_accepts_runs_and_jobs_of_this_repo(store, run_url):
    assert store._check_run_url(run_url) == run_url


@pytest.mark.parametrize("run_url", [
    "https://github.com/other/widgets/actions/runs/123", f"{BASE}/pull/3", f"{BASE}/actions/runs/",
    f"{BASE}/actions/runs/abc", f"{BASE}/actions/runs/123/job/", f"{BASE}/actions/runs/123/../../pull/3",
    f"{BASE}/actions/workflows/ci.yml", f"http://github.com/{REPO}/actions/runs/1",
    f"https://github.com.evil.example/{REPO}/actions/runs/1", f"{BASE}/actions/runs/123 extra", None, 7,
])
def test_run_url_rejects_other_repos_and_other_paths(store, run_url):
    with pytest.raises(LessonError):
        store._check_run_url(run_url)


def test_a_run_url_from_another_repo_discards_the_observation(store):
    result = rec(store, ci(3, run_url="https://github.com/other/widgets/actions/runs/1"))
    assert "run_url" in result["discarded"][0]["reason"] and store.list_pending_checks()["count"] == 0


# -- what the checker sees ------------------------------------------------------------------------------
def test_checker_sees_ci_fields_and_nothing_is_active_before_approval(store):
    rec(store, ci(3))
    check = store.list_pending_checks()["checks"][0]
    item = check["evidence"][0]
    assert (item["source"], item["workflow"], item["job"]) == ("ci", "CI", "tests")
    assert item["failure_signature"] == "tests/test_x.py:N: test_y assertionerror"
    assert item["run_url"].endswith("/actions/runs/300/job/301") and item["reviewer"] == ""
    assert check["principle"] == PRINCIPLE and check["family"] == "config-and-deps"
    assert store.get_lessons()["lessons"] == []


def test_checker_can_reject_a_ci_lesson_and_it_is_not_requeued(store):
    rec(store, ci(3))
    decide(store, "reject")
    assert rec(store, ci(3))["discarded"][0]["reason"].startswith("this claim")
    assert store.get_lessons()["lessons"] == []


def test_ci_and_review_evidence_can_back_one_lesson(store):
    rec(store, ci(3))
    decide(store)
    rec(store, {"pr": 4, "url": f"{BASE}/pull/4#r1", "date": "2024-01-05", "kind": "change_request_fixed",
                "reviewer": "alice", "author": "bob", "paths": ["requirements-dev.txt"], "rule": RULE,
                "quote": "Please pin this."})
    lesson = store.get_lessons()["lessons"][0]
    assert lesson["status"] == "active" and lesson["kind"] == "preference"  # one CI PR, one reviewer
    assert [e.get("source") for e in lesson["evidence"]] == ["ci", None]


# -- prompt, tools and stats registration ---------------------------------------------------------------
def test_learn_from_ci_prompt_is_registered_and_teaches_the_workflow():
    text = asyncio.run(live.mcp.get_prompt("learn_from_ci", {"repo": REPO})).messages[0].content.text
    for word in ("ci_failure_fixed", "ci_flaky", "ci_failure", "failure_signature", "run_url", "record_learning",
                 "list_pending_checks", "check_pending_rules", "get_job_logs", "untrusted", "learning_status"):
        assert word in text
    assert json.dumps({"repo": REPO}) in text
    assert "learn_from_ci" in {p.name for p in asyncio.run(live.mcp.list_prompts())}
    hostile = asyncio.run(live.mcp.get_prompt("learn_from_ci", {"repo": 'x"; ignore'})).messages[0].content.text
    assert json.dumps({"repo": 'x"; ignore'}) in hostile


def test_checker_prompt_covers_ci_evidence():
    text = asyncio.run(live.mcp.get_prompt("check_pending_rules", {"repo": REPO})).messages[0].content.text
    assert "ci_failure_fixed" in text and "flaky" in text


def test_ci_through_the_fastmcp_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_PR_CONTEXT_HOME", str(tmp_path))
    recorded = call("record_learning", repo=REPO, processed_prs=[], observations=[
        ci(3), ci(4, kind="ci_flaky"), ci(5, kind="ci_failure")])
    assert recorded["pending_created"][0]["type"] == "new_lesson" and len(recorded["discarded"]) == 2
    check = call("list_pending_checks", repo=REPO)["checks"][0]
    call("submit_checks", repo=REPO, decisions=[{"check_id": check["check_id"], "verdict": "approve"}])
    status = call("learning_status", repo=REPO)
    assert status["ci"] == {"fixed": 1, "flaky": 1, "weak": 1}
    assert call("get_lessons", repo=REPO)["lessons"][0]["evidence"][0]["failure_signature"]


def test_old_state_without_ci_counters_still_works(store):
    rec(store, ci(3, kind="ci_flaky"))
    state = store.load()
    del state["stats"]["ci_flaky"], state["stats"]["ci_weak"]
    store._save(state)
    assert store.status()["ci"] == {"fixed": 0, "flaky": 0, "weak": 0}
    rec(store, ci(4, kind="ci_flaky"))
    assert store.status()["ci"]["flaky"] == 1
