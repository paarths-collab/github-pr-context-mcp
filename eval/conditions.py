"""Retrieval conditions for the offline evaluation harness.

Every condition answers ``search(repo, query)`` with a ranked list of UNIT hits::

    {"id": evidence_id, "metadata": {...unit metadata...}, "score": float,
     "supported": bool, "match": str | None}

``supported`` is the abstention rule: a hit is "above threshold".  The runner
keeps supported hits only, lifts them to threads (metadata ``source_key``) and a
condition that has none left has abstained.

Conditions
----------
keyword    local BM25 over the same stored documents (no storage, no model).
semantic   storage.vector_store.query_similar over a tmp Chroma index.
hybrid     app.retrieval.search_evidence (semantic + exact, fused) over the same index.
oracle     returns the labelled relevant threads (separates retrieval from downstream misuse).

Importing this module is cheap; storage / chromadb / the encoder are imported
only inside ``open_index``.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import shutil
import sys
import tempfile
from collections import Counter

from storage.document_builder import build_documents

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by do does for from how in is it of on or our so that the this to was we "
    "what when where which who why with should would could can".split()
)
BM25_K1 = 1.5
BM25_B = 0.75
RETRIEVE_UNITS = 20      # unit hits requested from the index
RETRIEVE_THREADS = 10    # threads kept after de-duplication
FAKE_DIM = 256

# Uncalibrated placeholders.  Choose real thresholds on a development split,
# never on the scored questions, and record them (they are echoed in the output).
DEFAULT_THRESHOLDS = {
    "fake": {"keyword": 1.0, "semantic": 0.25},
    "minilm": {"keyword": 1.0, "semantic": 0.30},
}


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS]


def thread_id_of(metadata: dict, fallback: str = "") -> str:
    """The label key of a unit: its source thread / PR description / commit / review id."""
    return str((metadata or {}).get("source_key") or fallback)


# ── deterministic offline encoder ─────────────────────────────────────────────

class FakeEncoder:
    """Feature-hashed bag of words, L2-normalised.  Deterministic, no download.

    It is NOT a semantic model: it only rewards shared words.  It exists so the
    storage/retrieval plumbing can be exercised offline and in CI.
    """

    def __init__(self, dim: int = FAKE_DIM):
        self.dim = dim

    def encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token, count in Counter(tokenize(text)).items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[0] = 1.0  # empty text: a fixed unit vector keeps cosine defined
            return vector
        return [v / norm for v in vector]

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.encode(t) for t in texts]


# ── corpus -> units ───────────────────────────────────────────────────────────

def build_units(prs_by_repo: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Run the product's own document builder: ``{repo: [{id, doc, metadata}]}``."""
    out: dict[str, list[dict]] = {}
    for repo, prs in prs_by_repo.items():
        docs, metas, ids = build_documents(prs)
        out[repo] = [{"id": i, "doc": d, "metadata": m} for i, d, m in zip(ids, docs, metas)]
    return out


def _hit(unit: dict, score: float, supported: bool, match: str | None = None) -> dict:
    return {"id": unit["id"], "metadata": unit["metadata"], "score": score,
            "supported": supported, "match": match}


# ── (c) keyword / BM25 baseline ───────────────────────────────────────────────

class KeywordCondition:
    name = "keyword"

    def __init__(self, units_by_repo: dict[str, list[dict]], threshold: float):
        self.threshold = threshold
        self._index: dict[str, dict] = {}
        for repo, units in units_by_repo.items():
            tokens = [Counter(tokenize(u["doc"])) for u in units]
            lengths = [sum(c.values()) for c in tokens]
            df: Counter = Counter()
            for counts in tokens:
                df.update(counts.keys())
            self._index[repo] = {
                "units": units, "tokens": tokens, "lengths": lengths, "df": df,
                "avg": (sum(lengths) / len(lengths)) if lengths else 1.0,
            }

    def search(self, repo: str, query: str, **_: object) -> list[dict]:
        idx = self._index.get(repo)
        if not idx:
            return []
        n = len(idx["units"])
        terms = list(dict.fromkeys(tokenize(query)))
        hits: list[dict] = []
        for unit, counts, length in zip(idx["units"], idx["tokens"], idx["lengths"]):
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                df = idx["df"][term]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                norm = tf + BM25_K1 * (1 - BM25_B + BM25_B * length / (idx["avg"] or 1.0))
                score += idf * tf * (BM25_K1 + 1) / norm
            if score > 0:
                hits.append(_hit(unit, score, score >= self.threshold))
        hits.sort(key=lambda h: (-h["score"], h["id"]))
        return hits[:RETRIEVE_UNITS]


# ── (d)/(e) real storage + retrieval over a tmp Chroma ────────────────────────

class OracleCondition:
    name = "oracle"

    def __init__(self, units_by_repo: dict[str, list[dict]]):
        self._units = units_by_repo

    def search(self, repo: str, query: str, *, relevant: list[str] | None = None, **_: object) -> list[dict]:
        hits: list[dict] = []
        for thread_id in relevant or []:
            for unit in self._units.get(repo, []):
                if thread_id_of(unit["metadata"]) == thread_id:
                    hits.append(_hit(unit, 1.0, True, "oracle"))
                    break
        return hits


class IndexedCondition:
    """semantic (query_similar) or hybrid (search_evidence) over the tmp index."""

    def __init__(self, name: str, index: "OpenIndex", units_by_repo: dict[str, list[dict]], threshold: float):
        self.name = name
        self.threshold = threshold
        self._index = index
        self._by_id = {(repo, u["id"]): u for repo, units in units_by_repo.items() for u in units}
        self.retrieval_modes: set[str] = set()
        self.warnings: list[str] = []

    def _semantic(self, repo: str, query: str) -> list[dict]:
        return self._index.query_similar(repo, query, RETRIEVE_UNITS)

    def search(self, repo: str, query: str, **_: object) -> list[dict]:
        semantic = self._semantic(repo, query)
        sims = {h["id"]: float(h["similarity"]) for h in semantic}
        if self.name == "semantic":
            return [
                _hit(self._by_id.get((repo, h["id"]), {"id": h["id"], "metadata": h["metadata"]}),
                     float(h["similarity"]), float(h["similarity"]) >= self.threshold, "semantic")
                for h in semantic
            ]
        envelope = self._index.search_evidence(repo, query)
        self.retrieval_modes.add(str(envelope.get("retrieval_mode")))
        for warning in envelope.get("warnings") or []:
            if warning not in self.warnings:
                self.warnings.append(str(warning))
        state = str(envelope.get("index_state"))
        if not (state.startswith("ready") or state == "stale"):
            raise RuntimeError(f"hybrid retrieval ran against index_state={state!r}")
        hits: list[dict] = []
        for unit in envelope.get("results") or []:
            evidence_id = unit["evidence_id"]
            stored = self._by_id.get((repo, evidence_id))
            if stored is None:
                raise RuntimeError(f"hybrid returned unknown evidence id {evidence_id!r}")
            match = unit.get("match")
            # Supported = an exact identifier/path/PR match, or semantic similarity above threshold.
            supported = match in {"exact", "both"} or sims.get(evidence_id, -1.0) >= self.threshold
            hits.append(_hit(stored, float(unit.get("score") or 0.0), supported, match))
        return hits


class OpenIndex:
    """Handles on the patched storage modules while ``open_index`` is active."""

    def __init__(self, vs, retrieval):
        self._vs = vs
        self._retrieval = retrieval

    def query_similar(self, repo: str, query: str, n: int) -> list[dict]:
        embedding = self._vs.encode(query)
        return self._vs.query_similar(repo, query, n_results=n, temporary=False, query_embedding=embedding)

    def search_evidence(self, repo: str, query: str) -> dict:
        return self._retrieval.search_evidence(
            repo, None, False, query, n_results=RETRIEVE_THREADS, budget_tokens=self._retrieval.BUDGET_MAX,
        )


@contextlib.contextmanager
def open_index(prs_by_repo: dict[str, list[dict]], encoder: str):
    """Index ``prs_by_repo`` (via storage.vector_store.index_prs) into a throw-away Chroma.

    ``encoder`` is "fake" (deterministic, offline) or "minilm" (the product's real
    SentenceTransformer; needs the model cached locally or network access).
    Everything is redirected to a temp directory and restored on exit; the user's
    real index and cursor database are never opened or written.
    """
    if encoder not in {"fake", "minilm"}:
        raise ValueError("encoder must be 'fake' or 'minilm'")
    tmp = tempfile.mkdtemp(prefix="pr-context-eval-")
    saved_env = {k: os.environ.get(k) for k in ("CHROMA_PERSIST_DIR", "CURSOR_DB_PATH")}
    # Set before the first import so a cold process never touches ~/.github-pr-mcp.
    os.environ["CHROMA_PERSIST_DIR"] = os.path.join(tmp, "chroma")
    os.environ["CURSOR_DB_PATH"] = os.path.join(tmp, "cursors.db")
    restore: list[tuple[object, str, object]] = []

    def patch(obj: object, attr: str, value: object) -> None:
        restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    try:
        import chromadb
        import storage as storage_pkg
        import storage.vector_store as vs
        import app.retrieval as retrieval
        cursor_module = sys.modules["storage.cursor_store"]

        store = cursor_module.CursorStore(os.path.join(tmp, "cursors.db"))
        patch(cursor_module, "cursor_store", store)
        patch(storage_pkg, "cursor_store", store)
        patch(vs, "_persistent_client", chromadb.PersistentClient(path=os.path.join(tmp, "chroma")))
        if encoder == "fake":
            fake = FakeEncoder()
            patch(vs, "encode", fake.encode)
            patch(vs, "encode_batch", fake.encode_batch)
            patch(retrieval, "encode", fake.encode)
        for repo, prs in prs_by_repo.items():
            vs.index_prs(repo, prs, temporary=False)
        yield OpenIndex(vs, retrieval)
    finally:
        for obj, attr, value in reversed(restore):
            setattr(obj, attr, value)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)
