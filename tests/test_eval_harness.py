"""Offline evaluation harness (eval/): metric math on hand-computed cases plus an
end-to-end run of the bundled INVENTED sample with the deterministic fake encoder.

No network and no model download: ``--encoder fake`` only.  The sample numbers are
a plumbing smoke test and are deliberately NOT asserted as quality targets.
"""

import copy
import json
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("chromadb", reason="ChromaDB is required for the end-to-end harness run")

from eval import conditions as cond  # noqa: E402
from eval import metrics, run_eval  # noqa: E402


# ── metric math (hand computed) ───────────────────────────────────────────────

class TestMetricMath:

    def test_recall_precision_rr_on_a_hand_computed_list(self):
        ranked = ["a", "b", "c", "d", "e", "f"]
        relevant = ["b", "f"]
        # top-5 holds b only: recall 1/2, precision 1/5; first relevant at rank 2.
        assert metrics.recall_at_k(ranked, relevant, 5) == pytest.approx(0.5)
        assert metrics.precision_at_k(ranked, relevant, 5) == pytest.approx(0.2)
        assert metrics.reciprocal_rank(ranked, relevant) == pytest.approx(0.5)
        # k=6 reaches f as well.
        assert metrics.recall_at_k(ranked, relevant, 6) == pytest.approx(1.0)
        assert metrics.precision_at_k(ranked, relevant, 6) == pytest.approx(2 / 6)

    def test_reciprocal_rank_looks_past_k(self):
        ranked = ["a", "b", "c", "d", "e", "f", "g"]
        assert metrics.reciprocal_rank(ranked, ["g"]) == pytest.approx(1 / 7)
        assert metrics.recall_at_k(ranked, ["g"], 5) == 0.0

    def test_no_relevant_hit_scores_zero(self):
        assert metrics.recall_at_k(["a"], ["z"], 5) == 0.0
        assert metrics.precision_at_k(["a"], ["z"], 5) == 0.0
        assert metrics.reciprocal_rank(["a"], ["z"]) == 0.0

    def test_empty_ranked_list_scores_zero_without_error(self):
        assert metrics.recall_at_k([], ["z"], 5) == 0.0
        assert metrics.reciprocal_rank([], ["z"]) == 0.0

    def test_precision_uses_a_fixed_denominator(self):
        # One result, and it is relevant: 1/5, not 1/1.
        assert metrics.precision_at_k(["a"], ["a"], 5) == pytest.approx(0.2)

    def test_recall_is_undefined_without_relevant_threads(self):
        with pytest.raises(ValueError):
            metrics.recall_at_k(["a"], [], 5)

    def test_score_question_answerable(self):
        row = metrics.score_question("conflicting", ["x", "a", "b"], ["a", "b", "c", "d"], 5)
        assert row["recall"] == pytest.approx(0.5)
        assert row["precision"] == pytest.approx(2 / 5)
        assert row["rr"] == pytest.approx(0.5)
        assert row["hit"] is True and row["abstained"] is False
        assert row["correct_abstention"] is None

    def test_score_question_unanswerable_is_abstention_only(self):
        abstained = metrics.score_question("unanswerable", [], [], 5)
        assert abstained["correct_abstention"] is True
        assert abstained["recall"] is None and abstained["precision"] is None and abstained["rr"] is None
        answered = metrics.score_question("unanswerable", ["a"], [], 5)
        assert answered["correct_abstention"] is False and answered["abstained"] is False

    def test_label_contract_violations_raise(self):
        with pytest.raises(ValueError):
            metrics.score_question("unanswerable", [], ["a"], 5)
        with pytest.raises(ValueError):
            metrics.score_question("rationale", [], [], 5)
        with pytest.raises(ValueError):
            metrics.score_question("not_a_category", [], ["a"], 5)


def _row(category, ranked, relevant, k=5):
    return {"category": category, **metrics.score_question(category, ranked, relevant, k)}


class TestSummaries:

    def test_unanswerable_questions_never_enter_recall(self):
        rows = [
            _row("rationale", ["a"], ["a"]),              # recall 1, P 1/5, RR 1
            _row("rationale", ["x", "b"], ["b", "c"]),    # recall 1/2, P 1/5, RR 1/2
            _row("unanswerable", [], []),                  # correct abstention
            _row("unanswerable", ["a"], []),               # wrongly answered
        ]
        summary = metrics.summarize(rows)
        assert summary["answerable"]["n"] == 2
        assert summary["answerable"]["recall"] == pytest.approx(0.75)
        assert summary["answerable"]["precision"] == pytest.approx(0.2)
        assert summary["answerable"]["mrr"] == pytest.approx(0.75)
        assert summary["answerable"]["false_abstention_rate"] == 0.0
        assert summary["unanswerable"]["n"] == 2
        assert summary["unanswerable"]["correct_abstention_rate"] == pytest.approx(0.5)

    def test_recall_is_unchanged_by_adding_unanswerable_rows(self):
        base = [_row("exact_identifier", ["a"], ["a"]), _row("conflicting", ["z"], ["a", "b"])]
        with_unanswerable = base + [_row("unanswerable", [], [])] * 5
        assert metrics.summarize(with_unanswerable)["answerable"]["recall"] == \
            metrics.summarize(base)["answerable"]["recall"] == pytest.approx(0.5)

    def test_false_abstention_counts_empty_lists_on_answerable_questions(self):
        rows = [_row("rationale", [], ["a"]), _row("rationale", ["a"], ["a"])]
        answerable = metrics.summarize(rows)["answerable"]
        assert answerable["false_abstention_rate"] == pytest.approx(0.5)
        assert answerable["recall"] == pytest.approx(0.5)
        assert answerable["hit_rate"] == pytest.approx(0.5)

    def test_missing_kind_is_none_not_zero(self):
        only_answerable = metrics.summarize([_row("rationale", ["a"], ["a"])])
        assert only_answerable["unanswerable"] == {"n": 0, "correct_abstention_rate": None}
        only_unanswerable = metrics.summarize([_row("unanswerable", [], [])])
        assert only_unanswerable["answerable"]["recall"] is None
        assert only_unanswerable["answerable"]["n"] == 0

    def test_by_category_tables_and_counts(self):
        rows = [
            _row("exact_identifier", ["a"], ["a"]),
            _row("rationale", ["x"], ["a"]),
            _row("rationale", ["a"], ["a"]),
            _row("unanswerable", [], []),
        ]
        out = metrics.summarize_by_category(rows)
        assert set(out["by_category"]) == {"exact_identifier", "rationale", "unanswerable"}
        assert out["by_category"]["rationale"]["answerable"]["n"] == 2
        assert out["by_category"]["rationale"]["answerable"]["recall"] == pytest.approx(0.5)
        assert out["by_category"]["exact_identifier"]["answerable"]["recall"] == 1.0
        assert out["by_category"]["unanswerable"]["unanswerable"]["n"] == 1
        assert out["overall"]["answerable"]["n"] == 3

    def test_markdown_marks_missing_values_na(self):
        table = metrics.markdown_tables(
            {"keyword": metrics.summarize_by_category([_row("unanswerable", [], [])])}, 5
        )
        assert "| keyword | 0 | n/a | n/a | n/a | n/a |" in table
        assert "| keyword | 1 | 1.000 |" in table
        assert "never averaged into recall" in table


# ── pieces of the pipeline ────────────────────────────────────────────────────

class TestFakeEncoder:

    def test_is_deterministic_and_unit_length(self):
        enc = cond.FakeEncoder()
        a = enc.encode("retry with exponential backoff")
        assert a == enc.encode("retry with exponential backoff")
        assert math.sqrt(sum(v * v for v in a)) == pytest.approx(1.0)
        assert len(a) == cond.FAKE_DIM

    def test_shared_words_raise_similarity(self):
        enc = cond.FakeEncoder()

        def cos(x, y):
            return sum(p * q for p, q in zip(enc.encode(x), enc.encode(y)))

        assert cos("balance cache ttl", "cache balance ttl") == pytest.approx(1.0)
        assert cos("balance cache ttl", "debounce search input") < 0.5

    def test_empty_text_is_still_a_valid_vector(self):
        assert math.sqrt(sum(v * v for v in cond.FakeEncoder().encode(""))) == pytest.approx(1.0)


def _tiny_corpus():
    corpus = run_eval.load_corpus(run_eval.SAMPLE_CORPUS)
    return corpus


def _question(**overrides):
    base = {
        "id": "t1", "category": "exact_identifier", "repo": "demo-org/ledger-service",
        "query": "retry_with_backoff", "relevant_thread_ids": ["PRRT_ledger_retry"], "notes": "",
    }
    base.update(overrides)
    return base


class TestKeywordAndLeakage:

    def test_bm25_ranks_the_thread_with_the_identifier_first(self):
        units = cond.build_units(_tiny_corpus()["repos"])
        hits = cond.KeywordCondition(units, threshold=1.0).search("demo-org/ledger-service", "retry_with_backoff")
        assert hits[0]["metadata"]["source_key"] == "PRRT_ledger_retry"
        assert hits[0]["supported"] is True
        assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)

    def test_threshold_decides_support_and_abstention(self):
        corpus = _tiny_corpus()
        q = _question(query="Why did we choose Kubernetes over Nomad for deployments?",
                      category="unanswerable", relevant_thread_ids=[])
        doc = run_eval.evaluate([q], corpus, conditions=("keyword",), thresholds={"keyword": 1.0, "semantic": 0.25})
        assert doc["results"]["keyword"]["questions"][0]["abstained"] is True
        # A zero threshold lets any token overlap through.
        loose = run_eval.evaluate([_question(query="ledger writer rewrite in Rust", category="unanswerable",
                                             relevant_thread_ids=[])], corpus, conditions=("keyword",),
                                  thresholds={"keyword": 0.0, "semantic": 0.25})
        assert loose["results"]["keyword"]["questions"][0]["abstained"] is False

    def test_old_comment_edited_after_query_time_is_excluded_by_the_conservative_cutoff(self):
        corpus = _tiny_corpus()
        thresholds = {"keyword": 1.0, "semantic": 0.25}
        q = _question(query="src/reports/export.py", category="exact_identifier",
                      relevant_thread_ids=["PRRT_ledger_decimal"])
        without = run_eval.evaluate([q], corpus, conditions=("keyword",), thresholds=thresholds)
        assert without["results"]["keyword"]["questions"][0]["returned_thread_ids"][0] == "PRRT_ledger_decimal"

        # The thread's first comment (January) was edited in August: a May cutoff must not admit it.
        timed = run_eval.evaluate([{**q, "query_time": "2024-05-01T00:00:00Z"}], corpus,
                                  conditions=("keyword",), thresholds=thresholds)
        row = timed["results"]["keyword"]["questions"][0]
        assert "PRRT_ledger_decimal" not in row["returned_thread_ids"]
        assert row["dropped_by_cutoff"] >= 1
        assert row["recall"] == 0.0
        info = timed["eligibility"]["t1"]
        assert info["excluded_by_reason"]["updated after cutoff"] == 1
        # The naive created-only cutoff would have admitted one more unit.
        assert info["eligible_units_created_only"] == info["eligible_units"] + 1

    def test_query_time_hides_later_reversals(self):
        corpus = _tiny_corpus()
        q = _question(query="Should account balances be cached?", category="conflicting",
                      relevant_thread_ids=["PRRT_ledger_cache_on"], query_time="2024-05-01T00:00:00Z")
        doc = run_eval.evaluate([q], corpus, conditions=("keyword",),
                                thresholds={"keyword": 1.0, "semantic": 0.25})
        returned = doc["results"]["keyword"]["questions"][0]["returned_thread_ids"]
        assert "PRRT_ledger_cache_on" in returned and "PRRT_ledger_cache_off" not in returned


class TestValidation:

    def _run(self, questions, corpus=None):
        return run_eval.evaluate(questions, corpus or _tiny_corpus(), conditions=("oracle",))

    def test_unknown_relevant_thread_is_rejected(self):
        with pytest.raises(run_eval.HarnessError, match="not found"):
            self._run([_question(relevant_thread_ids=["PRRT_nope"])])

    def test_thread_from_another_repo_is_rejected(self):
        with pytest.raises(run_eval.HarnessError, match="not found"):
            self._run([_question(relevant_thread_ids=["PRRT_web_debounce"])])

    def test_unanswerable_with_labels_is_rejected(self):
        with pytest.raises(run_eval.HarnessError, match="unanswerable"):
            self._run([_question(category="unanswerable")])

    def test_answerable_without_labels_is_rejected(self):
        with pytest.raises(run_eval.HarnessError, match="at least one"):
            self._run([_question(relevant_thread_ids=[])])

    def test_repo_is_required_when_the_corpus_has_several(self):
        with pytest.raises(run_eval.HarnessError, match="repo"):
            self._run([{k: v for k, v in _question().items() if k != "repo"}])

    def test_repo_defaults_when_the_corpus_has_one(self):
        corpus = copy.deepcopy(_tiny_corpus())
        corpus["repos"] = {"demo-org/ledger-service": corpus["repos"]["demo-org/ledger-service"]}
        doc = self._run([{k: v for k, v in _question().items() if k != "repo"}], corpus)
        assert doc["results"]["oracle"]["questions"][0]["repo"] == "demo-org/ledger-service"

    def test_duplicate_ids_bad_category_and_bad_time_are_all_reported_together(self):
        with pytest.raises(run_eval.HarnessError) as info:
            self._run([_question(), _question(), _question(id="t2", category="bogus"),
                       _question(id="t3", query_time="yesterday")])
        message = str(info.value)
        assert "duplicate id" in message and "category" in message and "query_time" in message

    def test_threshold_overrides_are_parsed_and_checked(self):
        assert run_eval.parse_thresholds("fake", ["keyword=2.5"])["keyword"] == 2.5
        with pytest.raises(run_eval.HarnessError):
            run_eval.parse_thresholds("fake", ["bogus=1"])
        with pytest.raises(run_eval.HarnessError):
            run_eval.parse_thresholds("fake", ["semantic=high"])


# ── end to end: the bundled sample through the real storage + retrieval path ──

@pytest.fixture(scope="module")
def sample_run():
    import storage.vector_store as vs

    before = (vs._persistent_client, vs.encode, vs.encode_batch)
    corpus = run_eval.load_corpus(run_eval.SAMPLE_CORPUS)
    questions = run_eval.load_questions(run_eval.SAMPLE_QUESTIONS)
    document = run_eval.evaluate(questions, corpus, encoder="fake")
    after = (vs._persistent_client, vs.encode, vs.encode_batch)
    return document, before, after, corpus


class TestSampleEndToEnd:

    def test_all_four_conditions_ran_on_every_question(self, sample_run):
        document, *_ = sample_run
        assert document["conditions"] == ["keyword", "semantic", "hybrid", "oracle"]
        assert document["question_counts"] == {
            "exact_identifier": 3, "rationale": 3, "conflicting": 3, "unanswerable": 3, "total": 12,
        }
        for name in document["conditions"]:
            rows = document["results"][name]["questions"]
            assert [r["id"] for r in rows] == [f"q{i:02d}" for i in range(1, 13)]

    def test_result_document_is_json_serialisable(self, sample_run):
        document, *_ = sample_run
        assert json.loads(json.dumps(document))["k"] == 5

    def test_oracle_separates_retrieval_from_misuse(self, sample_run):
        document, *_ = sample_run
        oracle = document["results"]["oracle"]["overall"]
        assert oracle["answerable"]["recall"] == 1.0
        assert oracle["answerable"]["mrr"] == 1.0
        assert oracle["answerable"]["false_abstention_rate"] == 0.0
        assert oracle["unanswerable"]["correct_abstention_rate"] == 1.0
        # P@5 has a fixed denominator: nine questions with 1,1,1,1,1,1,2,2,2 relevant threads.
        assert oracle["answerable"]["precision"] == pytest.approx((6 * 1 / 5 + 3 * 2 / 5) / 9)

    def test_unanswerable_rows_carry_no_recall(self, sample_run):
        document, *_ = sample_run
        for name in document["conditions"]:
            for row in document["results"][name]["questions"]:
                if row["category"] == "unanswerable":
                    assert row["recall"] is None and row["rr"] is None and row["relevant_thread_ids"] == []
                else:
                    assert isinstance(row["recall"], float)

    def test_overall_recall_equals_the_mean_over_answerable_rows_only(self, sample_run):
        document, *_ = sample_run
        for name in document["conditions"]:
            rows = [r for r in document["results"][name]["questions"] if r["category"] != "unanswerable"]
            expected = sum(r["recall"] for r in rows) / len(rows)
            assert document["results"][name]["overall"]["answerable"]["recall"] == pytest.approx(expected)
            assert document["results"][name]["overall"]["answerable"]["n"] == 9

    def test_hybrid_uses_the_real_envelope_and_finds_exact_identifiers(self, sample_run):
        document, *_ = sample_run
        hybrid = document["results"]["hybrid"]
        assert hybrid["retrieval_modes"] == ["semantic+lexical"]
        by_id = {r["id"]: r for r in hybrid["questions"]}
        assert by_id["q01"]["returned_thread_ids"][0] == "PRRT_ledger_retry"
        assert by_id["q03"]["returned_thread_ids"][0] == "PRRT_web_debounce"
        assert hybrid["by_category"]["exact_identifier"]["answerable"]["recall"] == 1.0

    def test_semantic_hits_come_from_the_tmp_index_only(self, sample_run):
        document, *_ = sample_run
        known = {
            cond.thread_id_of(u["metadata"])
            for units in cond.build_units(sample_run[3]["repos"]).values() for u in units
        }
        for name in ("semantic", "hybrid"):
            for row in document["results"][name]["questions"]:
                assert set(row["returned_thread_ids"]) <= known

    def test_query_time_question_records_its_eligible_corpus(self, sample_run):
        document, *_ = sample_run
        assert list(document["eligibility"]) == ["q06"]
        info = document["eligibility"]["q06"]
        assert info["total_units"] == 14
        assert info["eligible_units"] == 9 and info["eligible_units_created_only"] == 10
        assert info["excluded_by_reason"] == {"created after cutoff": 4, "updated after cutoff": 1}
        for name in ("keyword", "semantic", "hybrid"):
            q06 = next(r for r in document["results"][name]["questions"] if r["id"] == "q06")
            assert "PRRT_ledger_cache_off" not in q06["returned_thread_ids"]

    def test_storage_patches_are_restored(self, sample_run):
        _, before, after, _ = sample_run
        assert before == after

    def test_the_real_index_directory_is_not_used(self, sample_run):
        import storage.vector_store as vs
        document, *_ = sample_run
        # The run indexed into a temp dir; the module-level client is the one it started with.
        assert document["encoder"] == "fake"
        assert vs._persistent_client is sample_run[1][0]

    def test_markdown_states_the_label_and_the_unanswerable_rule(self, sample_run):
        document, _, _, corpus = sample_run
        text = run_eval.render_markdown(document, corpus["label"])
        assert "INVENTED" in text and "NOT a benchmark" in text
        assert "never enter recall" in text
        for name in document["conditions"]:
            assert f"| {name} |" in text


class TestCommandLine:

    def test_writes_json_and_markdown(self, tmp_path, capsys):
        out, md = tmp_path / "r.json", tmp_path / "r.md"
        code = run_eval.main(["--conditions", "keyword,oracle", "--out", str(out), "--markdown", str(md)])
        assert code == 0
        document = json.loads(out.read_text())
        assert document["conditions"] == ["keyword", "oracle"]
        assert len(document["inputs"]["questions"]["sha256"]) == 64
        assert document["inputs"]["corpus"]["repos"] == {"demo-org/ledger-service": 7, "demo-org/web-client": 5}
        assert "### Overall" in md.read_text()
        assert "### Overall" in capsys.readouterr().out

    def test_custom_files_are_accepted(self, tmp_path):
        corpus = run_eval.load_corpus(run_eval.SAMPLE_CORPUS)
        corpus["label"] = "custom"
        corpus_path = tmp_path / "corpus.json"
        corpus_path.write_text(json.dumps(corpus))
        questions_path = tmp_path / "q.jsonl"
        questions_path.write_text(json.dumps(_question()) + "\n\n")
        out = tmp_path / "r.json"
        code = run_eval.main(["--questions", str(questions_path), "--corpus", str(corpus_path),
                              "--conditions", "keyword,oracle", "--out", str(out)])
        assert code == 0
        assert json.loads(out.read_text())["question_counts"]["total"] == 1

    def test_bad_labels_exit_with_an_error_code(self, tmp_path, capsys):
        questions_path = tmp_path / "q.jsonl"
        questions_path.write_text(json.dumps(_question(relevant_thread_ids=["PRRT_nope"])) + "\n")
        code = run_eval.main(["--questions", str(questions_path), "--conditions", "oracle"])
        assert code == 2
        assert "not found" in capsys.readouterr().err

    def test_unknown_condition_exits_with_an_error_code(self, capsys):
        assert run_eval.main(["--conditions", "bogus"]) == 2

    def test_missing_file_exits_with_an_error_code(self, tmp_path):
        assert run_eval.main(["--questions", str(tmp_path / "missing.jsonl")]) == 2


class TestSampleFiles:

    def test_sample_is_labelled_as_invented_smoke_data(self):
        corpus = run_eval.load_corpus(run_eval.SAMPLE_CORPUS)
        assert "INVENTED" in corpus["label"] and "NOT a benchmark" in corpus["label"]

    def test_sample_covers_every_category_with_three_questions(self):
        questions = run_eval.load_questions(run_eval.SAMPLE_QUESTIONS)
        assert {c: sum(q["category"] == c for q in questions) for c in metrics.CATEGORIES} == {
            "exact_identifier": 3, "rationale": 3, "conflicting": 3, "unanswerable": 3,
        }
        for q in questions:
            assert set(q) >= {"id", "category", "query", "relevant_thread_ids", "notes"}
