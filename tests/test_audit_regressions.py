"""Offline regressions for index publication and the public evidence contract."""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import math
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

chromadb = pytest.importorskip("chromadb")

from app import index_status, retrieval  # noqa: E402
from app.tools import analysis  # noqa: E402
import storage  # noqa: E402
import storage.vector_store as vs  # noqa: E402
from storage.cursor_store import CursorStore  # noqa: E402
from storage.document_builder import as_of_filter, build_documents  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPO = "example/audit"
cursor_module = importlib.import_module("storage.cursor_store")


def pr(number=1, body="Use retries for transient failures.", **extra):
    return {
        "number": number, "title": "Retry policy", "body": body, "author": "alice",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-03T00:00:00Z",
        "state": "MERGED", **extra,
    }


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    store = CursorStore(str(tmp_path / "cursors.db"))
    monkeypatch.setattr(cursor_module, "cursor_store", store)
    monkeypatch.setattr(storage, "cursor_store", store)
    monkeypatch.setattr(vs, "_persistent_client", chromadb.PersistentClient(path=str(tmp_path / "chroma")))
    monkeypatch.setattr(vs, "encode_batch", lambda texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(vs, "encode", lambda text: [1.0, 0.0])
    monkeypatch.setattr(retrieval, "encode", lambda text: [1.0, 0.0])
    monkeypatch.setattr(index_status, "_INDEX_JOBS", {})
    return store


# Run real independent storage instances against the same disposable Chroma/SQLite.
WRITER = r'''
import json, os, time
from pathlib import Path
import storage.vector_store as vs
vs.encode_batch = lambda texts: [[1.0, 0.0] for _ in texts]
label, control = os.environ['WRITER_LABEL'], Path(os.environ['CONTROL_DIR'])
original, first = vs._existing_generations, True
def existing(*args):
    global first
    result = original(*args)
    if first:
        first = False
        (control / (label + '.entered')).touch()
        if label == 'A':
            deadline = time.monotonic() + 60
            while not (control / 'release').exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('writer was not released')
                time.sleep(0.02)
    return result
vs._existing_generations = existing
(control / (label + '.started')).touch()
vs.index_prs('example/audit', [{'number': 2 if label == 'A' else 3,
    'body': 'Independent writer ' + label, 'state': 'MERGED'}])
print(json.dumps({'generation': vs.get_active_generation('example/audit')}))
'''


def wait_file(path, process, timeout=60):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            out, err = process.communicate()
            pytest.fail(f"writer exited early: {out}\n{err}")
        if time.monotonic() > deadline:
            pytest.fail(f"writer did not reach {path.name}")
        time.sleep(0.02)


def test_independent_writers_cannot_share_or_delete_a_published_stage(isolated, tmp_path):
    vs.index_prs(REPO, [pr()])
    env = {
        **os.environ, "CHROMA_PERSIST_DIR": str(tmp_path / "chroma"),
        "CURSOR_DB_PATH": str(tmp_path / "cursors.db"), "CONTROL_DIR": str(tmp_path),
        "ANONYMIZED_TELEMETRY": "false", "PYTHONDONTWRITEBYTECODE": "1",
    }
    processes = []
    try:
        a = subprocess.Popen([sys.executable, "-c", WRITER], cwd=ROOT,
                             env={**env, "WRITER_LABEL": "A"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(a)
        wait_file(tmp_path / "A.entered", a)
        b = subprocess.Popen([sys.executable, "-c", WRITER], cwd=ROOT,
                             env={**env, "WRITER_LABEL": "B"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(b)
        wait_file(tmp_path / "B.started", b)
        time.sleep(0.2)
        assert not (tmp_path / "B.entered").exists(), "another process entered the writer's critical section"
        (tmp_path / "release").touch()
        for process in processes:
            out, err = process.communicate(timeout=60)
            assert process.returncode == 0, f"{out}\n{err}"
        assert isolated.get_registry(REPO)["active_generation"] == 3
        assert vs.get_active_generation(REPO) == 3
        assert vs.get_collection_stats(REPO)["total_documents"] == 3
    finally:
        (tmp_path / "release").touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_delete_epoch_survives_a_new_storage_instance(isolated):
    epoch = vs.get_index_epoch(REPO)
    vs.delete_repo_index(REPO, storage="permanent")
    restarted = CursorStore(str(isolated._path))
    assert restarted.get_epoch(REPO) > epoch
    with pytest.raises(vs.IndexInvalidatedError):
        vs.index_prs(REPO, [pr()], expected_epoch=epoch)


def test_every_large_thread_part_is_reachable_through_the_registered_tool(isolated, monkeypatch):
    record = pr(body="", threads=[{
        "thread_id": "thread-long", "path": "src/retry.py",
        "comments": [{"id": f"c{i}", "author": "alice", "body": (f"Retry policy detail {i} " * 2100),
                      "created_at": "2024-01-02T00:00:00Z"} for i in range(2)],
    }])
    vs.index_prs(REPO, [record])
    _, _, ids = build_documents([record])
    tools = {}
    mcp = SimpleNamespace(tool=lambda *, name: lambda fn: tools.setdefault(name, fn))
    analysis.register_analysis_tools(mcp)
    monkeypatch.setattr(analysis, "get_state", lambda ctx: {"storage_types": {}})
    monkeypatch.setattr(analysis, "resolve_repo", lambda *a, **kw: REPO)
    monkeypatch.setattr(analysis, "resolve_namespace", lambda *a: None)
    monkeypatch.setattr(analysis, "track_usage", lambda *a: None)
    monkeypatch.setattr(analysis, "is_temporary", lambda *a: False)
    found, offset = [], 0
    for _ in range(len(ids)):
        result = json.loads(asyncio.run(tools["get_evidence"](
            evidence_id=ids[-1], part_offset=offset, ctx=object(),
        )))
        assert result["results"]
        found.extend(u["evidence_id"] for u in result["results"])
        page = result["pagination"]
        if not page["has_more"]:
            break
        assert page["next_part_offset"] > offset
        offset = page["next_part_offset"]
    assert found == ids
    assert len(ids) > len(result["results"])
    with pytest.raises(ValueError, match="part_offset"):
        retrieval.fetch_evidence(REPO, None, False, ids[0], part_offset=-1)
    blocked = retrieval.fetch_evidence(REPO, None, False, ids[0], budget_tokens=256)
    assert blocked["results"] == [] and blocked["pagination"]["has_more"]
    assert blocked["pagination"]["next_part_offset"] is None
    assert blocked["pagination"]["minimum_budget_tokens"] > 256


def test_nested_truncation_is_durable_and_visible_after_restart(isolated, monkeypatch):
    limits = ["files", "reviews", "commits"]
    vs.index_prs(REPO, [pr(truncated_connections=limits)])
    vs.set_backfill_state(REPO, history_complete=True)
    restarted = CursorStore(str(isolated._path))
    monkeypatch.setattr(cursor_module, "cursor_store", restarted)
    result = retrieval.search_evidence(REPO, None, False, "retries")
    assert result["coverage"]["truncated_connections"] == sorted(limits)
    assert "nested connections remain incomplete" in result["coverage"]["summary"]
    unit = result["results"][0]
    assert unit["source_truncated"] is True
    assert unit["truncated_connections"] == sorted(limits)
    assert all(limit in " ".join(result["warnings"]) for limit in limits)



def test_upgrade_recovers_truncation_from_preexisting_documents(isolated, monkeypatch):
    vs.index_prs(REPO, [pr(truncated_connections=["reviews", "files"])])
    # Recreate the previous ledger schema, retaining its data. NULL in the new
    # column must mean unknown, rather than falsely claiming a complete fetch.
    with sqlite3.connect(isolated._path) as conn:
        conn.execute("ALTER TABLE pr_ledger RENAME TO saved_ledger")
        conn.execute("""CREATE TABLE pr_ledger (
            repo_key TEXT NOT NULL, namespace TEXT NOT NULL, storage TEXT NOT NULL,
            generation INTEGER NOT NULL, pr_number INTEGER NOT NULL,
            created_at TEXT, updated_at TEXT, state TEXT,
            nested_incomplete_threads INTEGER DEFAULT 0,
            PRIMARY KEY (repo_key, namespace, storage, generation, pr_number))""")
        conn.execute("""INSERT INTO pr_ledger SELECT repo_key, namespace, storage,
            generation, pr_number, created_at, updated_at, state,
            nested_incomplete_threads FROM saved_ledger""")
        conn.execute("DROP TABLE saved_ledger")
    restarted = CursorStore(str(isolated._path))
    monkeypatch.setattr(cursor_module, "cursor_store", restarted)
    assert restarted.get_ledger(REPO)[0]["truncation_known"] is False
    assert vs.compute_coverage(REPO)["truncated_connections"] == ["files", "reviews"]
    # Carrying the old ledger into another generation must retain the warning.
    vs.index_prs(REPO, [pr(2)])
    assert vs.compute_coverage(REPO)["truncated_connections"] == ["files", "reviews"]


def test_publication_during_encoding_refreshes_the_response_snapshot(isolated, monkeypatch):
    vs.index_prs(REPO, [pr(body="Original retry policy")])

    def encode_and_publish(query):
        vs.index_prs(REPO, [pr(body="Replacement retry policy")])
        return [1.0, 0.0]

    monkeypatch.setattr(retrieval, "encode", encode_and_publish)
    result = retrieval.search_evidence(REPO, None, False, "retry policy")
    assert result["generation"] == result["coverage"]["generation"] == 2
    assert [u["excerpt"] for u in result["results"]] == ["Replacement retry policy"]


def test_publication_cannot_split_the_semantic_and_lexical_reads(isolated, monkeypatch):
    vs.index_prs(REPO, [pr(body="Original retry policy")])
    started, finished = threading.Event(), threading.Event()
    original = retrieval.query_similar
    errors = []

    def write():
        started.set()
        try:
            vs.index_prs(REPO, [pr(body="Replacement retry policy")])
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    writer = threading.Thread(target=write)

    def semantic(*args, **kwargs):
        hits = original(*args, **kwargs)
        writer.start()
        assert started.wait(5)
        assert not finished.wait(0.1)
        return hits

    monkeypatch.setattr(retrieval, "query_similar", semantic)
    try:
        result = retrieval.search_evidence(REPO, None, False, "retry policy")
    finally:
        writer.join(timeout=10)
    assert finished.is_set() and not errors
    assert result["generation"] == result["coverage"]["generation"] == 1
    assert all(u["excerpt"] == "Original retry policy" for u in result["results"])
    assert vs.get_active_generation(REPO) == 2


def test_public_evidence_excludes_edits_after_the_historical_cutoff(isolated):
    vs.index_prs(REPO, [pr(body="", threads=[{
        "thread_id": "thread-edited", "path": "src/retry.py", "comments": [
            {"id": "c1", "author": "alice", "body": "Use the new behavior added in June.",
             "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-06-01T00:00:00Z"},
            {"id": "c2", "author": "bob", "body": "Please add tests for the retry policy.",
             "created_at": "2024-01-02T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z"},
        ],
    }])])
    result = retrieval.search_evidence(REPO, None, False, "retry policy")
    unit = json.loads(retrieval.dump_envelope(result))["results"][0]
    assert unit["latest_comment_updated_at"] == unit["updated_at"] == "2024-06-01T00:00:00Z"
    assert unit["body_sha256"] == hashlib.sha256(unit["excerpt"].encode()).hexdigest()
    kept, dropped = as_of_filter([unit], "2024-03-01T00:00:00Z")
    assert not kept and dropped[0]["reason"] == "updated after cutoff"


@pytest.mark.parametrize("legacy_key", [None, "historical_context", "rules_material"])
def test_budget_counts_all_evidence_copies_and_reports_total_wire_size(isolated, legacy_key):
    vs.index_prs(REPO, [pr(i, body="Retry policy detail " * 30) for i in range(1, 5)])
    emitted = asyncio.run(retrieval.evidence_response(
        repo_key=REPO, namespace=None, temporary=False, query="retry policy",
        n_results=4, budget_tokens=1600, legacy_key=legacy_key,
    ))
    result = json.loads(emitted)
    copies = 2 if legacy_key else 1
    assert result["results"]
    assert result["budget"]["scope"] == "evidence_units"
    assert result["budget"]["used_tokens"] == copies * sum(retrieval.estimate_tokens(u) for u in result["results"])
    assert result["budget"]["used_tokens"] <= 1600
    assert result["budget"]["estimated_response_tokens"] == math.ceil(len(emitted) / 4)
    if legacy_key:
        assert result["results"] == result[legacy_key]


def test_install_probe_cannot_open_the_inherited_registry(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("audit_e2e_harness", ROOT / "tests/e2e/e2e_harness.py")
    harness = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, harness)
    spec.loader.exec_module(harness)
    parent_db = tmp_path / "user-registry.db"
    parent_db.write_bytes(b"This is the user's untouched registry sentinel.")
    before = parent_db.read_bytes()
    monkeypatch.setenv("CURSOR_DB_PATH", str(parent_db))
    monkeypatch.setenv("E2E_VENV", str(tmp_path / "reused-venv"))
    original = subprocess.run
    captured = {}

    def probe(command, **kwargs):
        captured.update(kwargs.get("env") or {})
        # Exercise the real storage import in a child; no installed wheel needed
        # for this unit test. Keep the harness's actual environment and cwd.
        code = (
            "import sys; sys.path.insert(0, " + repr(str(ROOT)) + "); import storage; "
            "print('site-packages/app/__init__.py'); print('site-packages/storage/__init__.py'); print('test')"
        )
        return original([sys.executable, "-I", "-c", code], **kwargs)

    monkeypatch.setattr(harness.subprocess, "run", probe)
    workdir = tmp_path / "probe-run"
    workdir.mkdir()
    harness.fresh_install(workdir, SimpleNamespace(say=lambda *a: None, facts={}))
    assert parent_db.read_bytes() == before
    assert Path(captured["CURSOR_DB_PATH"]).is_relative_to(workdir)
    assert Path(captured["CURSOR_DB_PATH"]).exists()
    assert captured["USAGE_TRACKING_ENABLED"] == "false"
