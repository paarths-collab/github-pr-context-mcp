# Offline retrieval evaluation harness

This directory measures **retrieval only**: given a question, does the server surface the review
threads a human labeller marked as relevant, and does it stay silent when none exist? No LLM is
called, no network is used, and nothing here judges downstream answers.

> **The bundled `sample/` files are INVENTED data for a harness smoke test. They are not a
> benchmark. Any number printed from them says only that the plumbing works.** No benchmark result
> is claimed anywhere in this repository.

## Run it

```bash
# smoke test on the invented sample: deterministic fake encoder, no model, no network
python eval/run_eval.py

# same files with the product's real MiniLM encoder (needs the model in the local HF cache)
HF_HUB_OFFLINE=1 python eval/run_eval.py --encoder minilm

# your own labelled files
python eval/run_eval.py --questions my/questions.jsonl --corpus my/corpus.json \
    --encoder minilm --out results.json --markdown results.md
```

Options: `--conditions keyword,semantic,hybrid,oracle` (subset), `--k 5`,
`--threshold keyword=1.0 --threshold semantic=0.3` (repeatable), `--out`, `--markdown`.
Exit code 2 means the input files are invalid (the message lists every problem).

Standalone runs index into a temporary directory; your real Chroma index and cursor database are
not opened for writing. Everything is removed afterwards.

## Conditions

| name | what it does |
|---|---|
| `keyword` (c) | Local BM25 (k1 1.5, b 0.75) over the same stored documents the index holds. No storage, no model. |
| `semantic` (d) | `storage.vector_store.query_similar` over a temporary Chroma index built with `index_prs`. The encoder is `--encoder`. |
| `hybrid` (e) | `app.retrieval.search_evidence` (semantic + exact identifier/path/PR-reference legs, fused) over the same index. |
| `oracle` | Returns the labelled relevant threads. It separates retrieval quality from what a downstream consumer does with good evidence: if a pipeline is wrong with the oracle, retrieval is not the cause. |

All index-backed conditions use the product's own `build_documents`, so the evidence units, ids and
metadata are exactly what production would store. Units are lifted to **threads** by their
`source_key` metadata (the thread id; for PR descriptions, commits and review summaries it is the
corresponding source id) and de-duplicated, best unit first, up to 10 per question.

`--encoder fake` is a feature-hashed bag of words: deterministic and offline, but not a semantic
model. It exists to test the plumbing, not to estimate semantic quality.

## Metrics (`metrics.py`)

For each **answerable** question (`exact_identifier`, `rationale`, `conflicting`), with `R` the
labelled relevant threads and `top-k` the first k returned threads (default k = 5):

* **Recall@5** = |top-5 ∩ R| / |R|. Conflicting questions list every side of the disagreement, so a
  condition that surfaces only one side scores 0.5.
* **Precision@5** = |top-5 ∩ R| / 5, with a fixed denominator. Its ceiling is below 1 when fewer than
  five threads are relevant; the `oracle` row shows that ceiling.
* **MRR** = mean over questions of 1 / rank of the first relevant thread in the whole returned list
  (0 if none).
* **False abstention** = share of answerable questions on which the condition returned nothing.

For **unanswerable** questions (`relevant_thread_ids` is empty):

* **Correct abstention rate** = share on which the condition returned nothing.

Unanswerable questions are **never** averaged into recall, precision or MRR, and the tables report
`n/a` (not 0) when a category has no questions of the relevant kind. Results are given overall and
per category, with counts.

### The abstention rule

A condition *abstains* when it returns no result above its threshold. Every unit hit carries a score
and a "supported" flag; only supported hits are kept. If none remain, the returned list is empty,
which is what the metrics see (so a false abstention on an answerable question costs recall).

| condition | a hit is supported when |
|---|---|
| keyword | BM25 score >= `keyword` threshold (default 1.0) |
| semantic | cosine similarity >= `semantic` threshold (default 0.25 fake, 0.30 minilm) |
| hybrid | its match is `exact` or `both` (an identifier, path or PR reference matched), or its cosine similarity >= `semantic` threshold |
| oracle | always (it abstains only when nothing is relevant) |

The defaults are uncalibrated placeholders. For a real run, set thresholds on a development split that
is disjoint from the scored questions, record them (they are written to the JSON output), and do not
tune them on the test questions. Fused (RRF) scores are not used for abstention because their scale
does not depend on relevance.

## File formats

### `questions.jsonl` (one JSON object per line)

```json
{"id": "q07", "category": "conflicting", "repo": "owner/name",
 "query": "Should account balances be cached?",
 "relevant_thread_ids": ["PRRT_xxx", "PRRT_yyy"],
 "notes": "free text for the adjudicator", "query_time": "2024-05-01T00:00:00Z"}
```

* `category`: `exact_identifier` | `rationale` | `conflicting` | `unanswerable`.
* `relevant_thread_ids`: ids from the corpus (`threads[].thread_id`; a PR description, commit or
  review summary is addressed by its evidence `source_key`). Must be non-empty for answerable
  categories and empty for `unanswerable`. Unknown ids are rejected.
* `repo`: required when the corpus has more than one repository.
* `query_time` (optional): the instant the question is being asked "as of". Evidence is then filtered
  with `storage.document_builder.as_of_filter`, which keeps a unit only if it was created **and** last
  updated at or before this time. `eligibility` in the JSON records how many units survive, and how
  many a created-only filter would have wrongly kept.

### `corpus.json`

```json
{"label": "text echoed into every report",
 "repos": {"owner/name": [ <PR record>, ... ]}}
```

A PR record has the shape `fetcher.transform.flatten_pr` returns (`number`, `title`, `body`,
`created_at`, `updated_at`, `threads[]` with `comments[]`, `commits`, `reviews`, ...). The harness feeds
it to `storage.vector_store.index_prs`, so any PR record the product can index can be evaluated. A
real corpus can be produced by running the product's fetcher against a repository and dumping the
flattened records.

## Historical leakage (why `query_time` exists)

A question asked "as of" a date must not be answered from evidence that did not exist then. Two
leaks matter:

1. Evidence **created** after the date. Filtering on `created_at` removes it.
2. Evidence **created before but edited after** the date. The old comment is still present, but its
   text may now say something it did not say at the time. Each unit therefore stores
   `body_sha256` (full sha256 of the verbatim excerpt) and, for threads, `latest_comment_updated_at`
   (the maximum `updated_at` over every comment, `""` when unknown). `as_of_filter` is conservative:
   it drops a unit whose latest known update is after the cutoff, and reports why in its dropped
   list. `created_only=True` reproduces the naive filter so the difference can be measured.

Conservative filtering can exclude a harmless typo edit. That is deliberate: it trades a little
recall for no leakage. Comparing the stored `body_sha256` against a re-fetched body is the follow-up
check that can restore such units.

## Labelling guide

Target protocol (to be run by people; nothing in this repository claims it has been done):
**48 questions over four repositories = 12 `exact_identifier`, 12 `rationale`, 12 `conflicting`,
12 `unanswerable`**, i.e. three per category per repository.

1. **Freeze the corpus first.** Choose the four repositories, fetch their pull-request history once
   and fix the file. Record the fetch time and each repository's date range in the corpus `label`.
   Labels refer to thread ids in that frozen file; never relabel against a later fetch.
2. **Write the questions before looking at any system's output.** Questions come from the labellers'
   own understanding of the repository, not from queries that a system answered well.
   * `exact_identifier`: names a function, class, config key, file path or PR number that appears
     verbatim in the evidence.
   * `rationale`: asks *why* something was done, phrased in words that do not copy the thread.
   * `conflicting`: the history disagrees or reverses itself; label **every** side that a careful
     reader needs (earlier decision and the one that overturned it).
   * `unanswerable`: plausible for the repository but not discussed in the frozen corpus. Include
     hard negatives that share vocabulary with real threads. A labeller must search the whole corpus
     (not just the ranked output of a system) before declaring a question unanswerable.
3. **Two labellers, independently.** Each labeller assigns `category` and `relevant_thread_ids` per
   question without seeing the other's labels or any system output. A thread is relevant if a careful
   reviewer would want it in front of them when answering; mere topical overlap is not enough.
4. **Adjudicate.** A third person (or the two labellers together) resolves every disagreement in
   category or in the relevant set, writes the reason in `notes`, and the adjudicated file is the only
   one the harness reads. Report inter-labeller agreement (for example Cohen's kappa on category and
   Jaccard overlap of relevant sets) **before** adjudication, together with the number of
   disagreements.
5. **Record the time protocol.** For each question store the query time (`query_time`) when the
   question is anchored to a point in history, and record the eligible corpus: the harness writes
   `eligibility` (total units, units eligible under the conservative cutoff, units a created-only
   cutoff would admit, exclusions by reason). Keep that block with the results.
6. **Freeze thresholds on a development split.** Hold out some questions (not part of the 48) to set
   the abstention thresholds, then run the 48 once. Archive the questions file, corpus file, their
   sha256 values (written to `inputs` in the JSON), the encoder, thresholds, `k` and the commit.
7. **Report honestly.** Give counts next to every rate (n = 12 per category per condition, or 3 per
   repository) and expect wide uncertainty at this size; show the oracle row; show unanswerable
   results separately; publish the labels and the disagreements.

## Output

`--out` writes JSON: `inputs` (paths, sha256 of both files, repo/PR counts), `encoder`, `thresholds`,
`k`, `question_counts`, `eligibility`, and `results[condition]` with `overall`, `by_category` and one
row per question (returned thread ids, per-question recall/precision/reciprocal rank, abstention).
`--markdown` writes the same summary as tables (also printed to stdout).

## Layout

| file | purpose |
|---|---|
| `run_eval.py` | CLI, input validation, ranking pipeline, output |
| `metrics.py` | pure metric functions and markdown tables |
| `conditions.py` | keyword baseline, fake encoder, temporary index, semantic/hybrid/oracle |
| `sample/questions.jsonl`, `sample/corpus.json` | invented smoke-test data |

`scripts/eval_harness.py` is a different, older tool (an LLM-judged A/B comparison). This directory
is independent of it.
