"""Shared harness for the stdio end-to-end test and ``scripts/e2e_stdio_smoke.py``.

It (1) builds the wheel from the working tree and installs it into a FRESH venv,
(2) launches the INSTALLED ``github-pr-context-mcp`` console script as a stdio MCP
server with a hermetic environment (see ``sitecustomize.py`` next to this file),
and (3) drives it with the official ``mcp`` client through the scripted flow in
``run_flow``, recording timings and a trimmed transcript.

Nothing here edits product code. Everything GitHub-shaped is invented fixture data.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

# BaseExceptionGroup is builtin from Python 3.11; anyio uses the exceptiongroup backport before that.
_EXCEPTION_GROUP = getattr(builtins, "BaseExceptionGroup", None)
if _EXCEPTION_GROUP is None:  # pragma: no cover - Python 3.10
    try:
        from exceptiongroup import BaseExceptionGroup as _EXCEPTION_GROUP
    except ImportError:
        _EXCEPTION_GROUP = ()

E2E_DIR = Path(__file__).resolve().parent
ROOT = E2E_DIR.parents[1]
REPO = "demo-org/demo-repo"
SCRATCH_REPO = "demo-org/demo-scratch"
CLIENT_ID = "Iv1.e2edummyclientid"
MINILM_DIR = "models--sentence-transformers--all-MiniLM-L6-v2"

KEY_TOOLS = {
    "ensure_repo_ready", "get_index_stats", "semantic_search_reviews", "get_evidence",
    "get_github_connection_status", "begin_github_authorization",
    "complete_github_authorization", "review_code_with_history",
    "generate_code_from_history", "get_team_review_patterns", "list_indexed_repos",
    "delete_repo_index", "find_similar_errors",
}
UNIT_KEYS = {
    "evidence_id", "source_url", "pr_url", "pr_number", "source_kind", "author", "author_type",
    "is_bot", "created_at", "updated_at", "observed_at", "original_commit", "original_path",
    "line", "excerpt", "thread_incomplete", "part_index", "part_count", "score", "match",
    "current_applicability",
}
ENVELOPE_KEYS = {
    "schema_version", "repository", "namespace", "generation", "index_state", "retrieval_mode",
    "coverage", "results", "candidate_conflicts", "budget", "warnings", "content_notice",
}
READY = {"ready_selected_scope", "ready_partial"}


# ── recording ─────────────────────────────────────────────────────────────────

def _trim(value, limit: int = 240, items: int = 6):
    """Shorten long strings / lists so a transcript stays readable."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...[+{len(value) - limit} chars]"
    if isinstance(value, list):
        out = [_trim(v, limit, items) for v in value[:items]]
        if len(value) > items:
            out.append(f"...[+{len(value) - items} more]")
        return out
    if isinstance(value, dict):
        return {k: _trim(v, limit, items) for k, v in value.items()}
    return value


@dataclass
class Recorder:
    verbose: bool = True
    started: float = field(default_factory=time.monotonic)
    steps: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    checks: int = 0
    failures: list = field(default_factory=list)

    def say(self, message: str) -> None:
        if self.verbose:
            print(f"[{time.monotonic() - self.started:7.1f}s] {message}", flush=True)

    def section(self, title: str) -> None:
        self.say(f"=== {title}")
        self.steps.append({"section": title})

    def check(self, condition, message: str) -> None:
        self.checks += 1
        if condition:
            self.say(f"  ok    {message}")
            return
        self.failures.append(message)
        self.say(f"  FAIL  {message}")
        raise AssertionError(message)

    def step(self, tool: str, args: dict, secs: float, output) -> None:
        self.steps.append({"tool": tool, "args": args, "secs": round(secs, 3), "output": _trim(output)})

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({
            "facts": self.facts, "timings_secs": self.timings, "checks": self.checks,
            "failures": self.failures, "steps": self.steps,
        }, indent=2, default=str), encoding="utf-8")


# ── fresh install ─────────────────────────────────────────────────────────────

def _run(cmd, **kw) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME"}}
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env, **kw)


def copy_tree(dest: Path) -> None:
    """Copy tracked + untracked-but-not-ignored files, so stale build/ output never leaks in."""
    names = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.split(b"\0")
    for raw in filter(None, names):
        rel = raw.decode()
        src = ROOT / rel
        if not src.is_file() or rel.endswith((".mp4", ".png")):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def fresh_install(workdir: Path, rec: Recorder) -> dict:
    """uv build the wheel, create a new venv (python 3.11), install the wheel into it."""
    reuse = os.environ.get("E2E_VENV")
    if reuse:
        venv = Path(reuse)
        rec.say(f"reusing prebuilt venv from E2E_VENV={venv} (no fresh install)")
    else:
        src, dist, venv = workdir / "src", workdir / "dist", workdir / "venv"
        t0 = time.monotonic()
        copy_tree(src)
        _run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=src)
        wheel = next(dist.glob("*.whl"))
        rec.timings["uv_build_secs"] = round(time.monotonic() - t0, 2)
        rec.say(f"built {wheel.name} in {rec.timings['uv_build_secs']}s")

        t0 = time.monotonic()
        _run(["uv", "venv", "--python", "3.11", str(venv)])
        _run(["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)])
        rec.timings["venv_and_install_secs"] = round(time.monotonic() - t0, 2)
        rec.say(f"fresh venv + wheel install in {rec.timings['venv_and_install_secs']}s")
        rec.facts["wheel"] = wheel.name

    python = venv / "bin" / "python"
    script = venv / "bin" / "github-pr-context-mcp"
    # Prove the INSTALLED copy is what imports: run from an empty cwd, no repo on the path.
    empty = workdir / "empty-cwd"
    empty.mkdir(exist_ok=True)
    probe = subprocess.run(
        [str(python), "-I", "-c",
         "import app, storage, importlib.metadata as m; "
         "print(app.__file__); print(storage.__file__); print(m.version('github-pr-context-mcp'))"],
        cwd=empty, check=True, capture_output=True, text=True,
    ).stdout.split("\n")
    info = {"venv": str(venv), "python": str(python), "script": str(script),
            "app_file": probe[0], "storage_file": probe[1], "version": probe[2]}
    rec.facts["install"] = info
    return info


# ── hermetic environment ──────────────────────────────────────────────────────

def hf_home() -> Path:
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")


def real_minilm_available() -> bool:
    snapshots = hf_home() / "hub" / MINILM_DIR / "snapshots"
    return snapshots.is_dir() and any(snapshots.glob("*/config.json"))


def choose_encoder() -> str:
    want = os.environ.get("E2E_ENCODER", "auto").lower()
    if want == "fake":
        return "fake"
    if want == "real":
        if not real_minilm_available():
            raise RuntimeError("E2E_ENCODER=real but MiniLM is not in the local Hugging Face cache.")
        return "real"
    return "real" if real_minilm_available() else "fake"


@dataclass
class Sandbox:
    root: Path
    encoder: str

    def __post_init__(self):
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.cwd = self.root / "cwd"          # deliberately has no .env
        for d in (self.home, self.state, self.cwd):
            d.mkdir(parents=True, exist_ok=True)
        self.keyring_file = self.state / "keyring.json"
        self.cursor_db = self.state / "cursors.db"
        self.chroma = self.state / "chroma"
        self.revoke_flag = self.state / "revoke-graphql"
        self.stub_log = self.state / "stub-log.jsonl"
        self.pid_file = self.state / "server.pid"
        self.server_log = self.state / "server-stderr.log"

    def env(self) -> dict:
        env = {
            "HOME": str(self.home),
            "CHROMA_PERSIST_DIR": str(self.chroma),
            "CURSOR_DB_PATH": str(self.cursor_db),
            "GITHUB_APP_CLIENT_ID": CLIENT_ID,
            "HF_HOME": str(hf_home()),      # real cache, read-only because of HF_HUB_OFFLINE
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHON_KEYRING_BACKEND": "e2e_keyring.FileKeyring",
            "E2E_KEYRING_FILE": str(self.keyring_file),
            "PYTHONPATH": str(E2E_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "E2E_STUB": "1",
            "E2E_ENCODER": self.encoder,
            "E2E_REVOKE_FLAG": str(self.revoke_flag),
            "E2E_STUB_LOG": str(self.stub_log),
            "E2E_PID_FILE": str(self.pid_file),
            "TOKENIZERS_PARALLELISM": "false",
            "ANONYMIZED_TELEMETRY": "False",   # chromadb's own posthog telemetry (not disabled by the product)
        }
        # Explicitly absent: GITHUB_TOKEN, TELEMETRY*, GITHUB_PR_CONTEXT_RUNTIME (the
        # entrypoint sets that itself). mcp's stdio_client also only forwards a small
        # whitelist of the parent environment, so nothing else leaks in.
        return env

    def stub_entries(self) -> list[dict]:
        if not self.stub_log.exists():
            return []
        return [json.loads(line) for line in self.stub_log.read_text().splitlines() if line.strip()]

    def registry(self, repo: str, storage: str = "permanent") -> dict | None:
        if not self.cursor_db.exists():
            return None
        conn = sqlite3.connect(f"file:{self.cursor_db}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM index_cursors WHERE repo_key=? AND storage=?", (repo, storage)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


# ── MCP client wrapper ────────────────────────────────────────────────────────

class Server:
    """One live stdio session; ``call`` returns parsed JSON when the tool returns JSON."""

    def __init__(self, session, sandbox: Sandbox, rec: Recorder):
        self.session, self.sb, self.rec = session, sandbox, rec

    async def call(self, tool: str, **args):
        t0 = time.monotonic()
        result = await self.session.call_tool(tool, args)
        secs = time.monotonic() - t0
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.isError:
            self.rec.step(tool, args, secs, {"isError": True, "text": text})
            raise AssertionError(f"{tool} returned an MCP error: {text[:400]}")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        self.rec.step(tool, args, secs, data if data is not None else text)
        self.rec.say(f"{tool}({_brief(args)}) -> {_summary(tool, data, text)}  [{secs:.2f}s]")
        return Reply(text, data, secs)

    async def poll_job(self, repo: str, timeout: float = 600.0) -> "Reply":
        """Poll get_index_stats until the background job has finished."""
        t0 = time.monotonic()
        while True:
            reply = await self.call("get_index_stats", repo=repo)
            job = (reply.data or {}).get("index_job") or {}
            if job.get("status") in {"ready", "partial", "failed", "cancelled"}:
                reply.waited = time.monotonic() - t0
                return reply
            if time.monotonic() - t0 > timeout:
                raise AssertionError(f"indexing of {repo} did not finish within {timeout}s: {reply.text[:300]}")
            await asyncio.sleep(1.0)

    def kill(self) -> int:
        pid = int(self.sb.pid_file.read_text())
        os.kill(pid, signal.SIGKILL)
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"server pid {pid} survived SIGKILL")
        return pid


@dataclass
class Reply:
    text: str
    data: dict | None
    secs: float
    waited: float = 0.0


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={_trim(v, 40)!r}" for k, v in args.items())


def _summary(tool: str, data, text: str) -> str:
    if isinstance(data, dict):
        if "results" in data and "index_state" in data:
            kinds = sorted({r["source_kind"] for r in data["results"]})
            return (f"{data['index_state']} gen={data['generation']} {len(data['results'])} results "
                    f"{kinds} conflicts={len(data['candidate_conflicts'])} mode={data['retrieval_mode']}")
        if "index_state" in data:
            cov = data.get("coverage") or {}
            return (f"{data['index_state']} gen={data.get('generation')} docs={data.get('total_documents')} "
                    f"prs={cov.get('pull_requests_indexed')} job={((data.get('index_job') or {}).get('status'))}")
        if "status" in data:
            return f"status={data['status']} connected={data.get('connected')}"
    return " ".join(text.split())[:140]


@contextlib.asynccontextmanager
async def open_server(info: dict, sandbox: Sandbox, rec: Recorder):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=info["script"], args=[], env=sandbox.env(), cwd=str(sandbox.cwd),
    )
    with open(sandbox.server_log, "ab") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=300)) as session:
                await session.initialize()
                yield Server(session, sandbox, rec)


# ── the scripted flow ─────────────────────────────────────────────────────────

def _kinds(env: dict) -> set:
    return {r["source_kind"] for r in env["results"]}


def _check_envelope(rec: Recorder, env: dict, label: str) -> None:
    rec.check(ENVELOPE_KEYS <= set(env), f"{label}: envelope has {sorted(ENVELOPE_KEYS - set(env)) or 'all'} keys")
    rec.check(env["schema_version"] == 1 and env["repository"] == REPO, f"{label}: schema_version 1, repository {REPO}")
    rec.check("untrusted" in env["content_notice"].lower(), f"{label}: content_notice marks excerpts untrusted")
    cov, budget = env["coverage"], env["budget"]
    rec.check({"summary", "older_history_remaining", "pull_requests_indexed", "generation"} <= set(cov),
              f"{label}: coverage block present")
    rec.check({"requested_tokens", "used_tokens", "omitted_units"} <= set(budget) and
              budget["used_tokens"] <= budget["requested_tokens"], f"{label}: budget block present and respected")
    for unit in env["results"]:
        missing = UNIT_KEYS - set(unit)
        rec.check(not missing, f"{label}: unit {unit.get('evidence_id', '?')[:18]} has all unit keys {missing or ''}")
        rec.check(unit["source_url"].startswith(f"https://github.com/{REPO}/"), f"{label}: source_url links into {REPO}")
        rec.check(unit["author_type"] in {"human", "bot", "unknown"} and unit["excerpt"].strip(),
                  f"{label}: author_type + non-empty excerpt")
        rec.check(unit["current_applicability"] == "not_assessed", f"{label}: current_applicability not_assessed")


async def run_flow(info: dict, sb: Sandbox, rec: Recorder) -> None:
    # ───────────────────────── session 1 ─────────────────────────
    rec.section("session 1: tools, GitHub auth")
    async with open_server(info, sb, rec) as srv:
        tools = {t.name for t in (await srv.session.list_tools()).tools}
        rec.facts["tool_count"] = len(tools)
        rec.say(f"list_tools -> {len(tools)} tools")
        rec.check(KEY_TOOLS <= tools, f"list_tools exposes the key tools (missing: {sorted(KEY_TOOLS - tools)})")

        status = (await srv.call("get_github_connection_status")).data
        rec.check(status["connected"] is False and status["status"] == "disconnected",
                  "fresh HOME: GitHub is disconnected")

        # Before connecting, indexing must fail loudly and searching must never look like 'no history'.
        pre = await srv.call("ensure_repo_ready", repo=REPO, storage="permanent", pages=1)
        rec.check("authorization_required" in pre.text, "ensure_repo_ready without a credential reports authorization_required")
        empty = (await srv.call("semantic_search_reviews", query="retry timeout", repo=REPO)).data
        rec.check(empty["results"] == [] and empty["index_state"] in {"failed", "no_index"},
                  "search before indexing: empty envelope with failed/no_index state")
        rec.check(any("NOT evidence" in w or "does not mean" in w for w in empty["warnings"]) and empty.get("action"),
                  "search before indexing: actionable warning, not a silent empty answer")

        begun = (await srv.call("begin_github_authorization")).data
        rec.check(begun["status"] == "authorization_pending" and begun["user_code"] == "E2E1-CODE"
                  and begun["verification_uri"] == "https://github.com/login/device",
                  "begin_github_authorization returns user_code + verification_uri")
        rec.check("device_code" not in json.dumps(begun) and "e2e-device-code" not in json.dumps(begun),
                  "begin_github_authorization never returns the device code")
        connected, polls = None, 0
        for _ in range(30):
            await asyncio.sleep(1.1)
            polls += 1
            done = (await srv.call("complete_github_authorization")).data
            if done.get("connected"):
                connected = done
                break
            rec.check(done["status"] == "authorization_pending", "complete_github_authorization pends until approved")
        rec.check(connected is not None and polls >= 2, f"authorization completed after {polls} polls (one pending first)")
        rec.check(connected["login"] == "demo-reviewer", "connected as the stub user")
        rec.check("e2e-access-token" not in json.dumps(connected), "connect result never contains the access token")
        rec.check(sb.keyring_file.exists() and "e2e-access-token" in sb.keyring_file.read_text(),
                  "credential was written to the file keyring backend (not an OS keychain)")

        # ── initial bounded import ──
        rec.section("session 1: initial bounded import (pages=1)")
        t0 = time.monotonic()
        started = await srv.call("ensure_repo_ready", repo=REPO, storage="permanent", pages=1)
        rec.check("bounded window" in started.text, "ensure_repo_ready announces a bounded window")
        done = await srv.poll_job(REPO)
        rec.timings["initial_index_secs"] = round(time.monotonic() - t0, 2)
        stats = done.data
        cov = stats["coverage"]
        rec.check(stats["index_state"] == "ready_selected_scope", f"initial import index_state is ready_selected_scope (got {stats['index_state']})")
        rec.check(cov["older_history_remaining"] is True and cov["history_complete"] is False,
                  "coverage.older_history_remaining is true, history_complete false")
        rec.check("bounded window" in (stats["message"] or "") or "bounded window" in cov["summary"] or
                  "not full" in cov["summary"], f"summary says bounded window: {cov['summary'][:110]}")
        rec.check(cov["pull_requests_indexed"] == 2 and (cov["pr_number_min"], cov["pr_number_max"]) == (105, 106),
                  "window holds exactly PRs #105-#106")
        rec.check(stats["total_documents"] > 0 and stats["generation"] >= 1, "documents indexed in generation >= 1")
        gen_initial = stats["generation"]

        # ── search ──
        rec.section("session 1: hybrid search, filters, budget, conflicts")
        q = "retry timeouts with backoff in HttpClient src/client.py"
        env = (await srv.call("semantic_search_reviews", query=q, repo=REPO, budget_tokens=16000)).data
        _check_envelope(rec, env, "search")
        rec.check(env["index_state"] == "ready_selected_scope" and env["generation"] == gen_initial, "search envelope reports state + generation")
        rec.check({"human_review_thread", "bot_review_thread"} <= _kinds(env), f"results include human and bot threads ({sorted(_kinds(env))})")
        rec.check("bot_review_summary" in _kinds(env) or env["budget"]["omitted_units"] == 0, "bot review summary is searchable")
        rec.check(any("older" in w.lower() and "not indexed" in w.lower() for w in env["warnings"]),
                  "warning: older history is not indexed")
        rec.check(any("bot" in w.lower() for w in env["warnings"]), "warning: bot-authored evidence present")
        by_id = {r["evidence_id"]: r for r in env["results"]}
        rec.check(len(env["candidate_conflicts"]) >= 1, "candidate_conflicts present for the retry reversal")
        conflict_prs = []
        for c in env["candidate_conflicts"]:
            rec.check(c["status"] == "unresolved" and all(i in by_id for i in c["evidence_ids"]) and c["basis"],
                      "conflict is unresolved, references returned units, has a basis")
            conflict_prs.append(sorted(by_id[i]["pr_number"] for i in c["evidence_ids"]))
        rec.check([105, 106] in conflict_prs, f"a conflict pairs PR #105 with the reversal in PR #106 ({conflict_prs})")
        rec.check("superseded" not in json.dumps(env["candidate_conflicts"]).lower(), "conflicts never claim 'superseded'")

        # Hostile repository text stays inside excerpts and is never acted on.
        hostile = [r for r in env["results"] if "IGNORE ALL PREVIOUS INSTRUCTIONS" in r["excerpt"]]
        outside = json.dumps({k: v for k, v in env.items() if k != "results"})
        rec.check(hostile and "IGNORE ALL PREVIOUS" not in outside,
                  "prompt-injection text only appears verbatim inside an excerpt")

        nobots = (await srv.call("semantic_search_reviews", query=q, repo=REPO, budget_tokens=16000, exclude_bots=True)).data
        rec.check(nobots["results"] and not any(r["is_bot"] or r["author_type"] == "bot" for r in nobots["results"]),
                  "exclude_bots=True removes every bot unit")
        rec.check(len(nobots["results"]) < len(env["results"]), f"exclude_bots shrinks the candidate set ({len(env['results'])} -> {len(nobots['results'])})")
        only = (await srv.call("semantic_search_reviews", query=q, repo=REPO, budget_tokens=16000,
                               source_kinds=["bot_review_summary"])).data
        rec.check(only["results"] and _kinds(only) == {"bot_review_summary"}, "source_kinds filter limits to bot_review_summary")
        tight = (await srv.call("semantic_search_reviews", query="cache TTL hit ratio retry", repo=REPO, budget_tokens=256)).data
        rec.check(tight["budget"]["used_tokens"] <= 256 and tight["budget"]["omitted_units"] >= 1,
                  f"budget_tokens=256 omits whole units ({tight['budget']})")

        before_unreachable = (await srv.call("semantic_search_reviews", query="tombstone record when invalidating a cache key",
                                             repo=REPO, budget_tokens=16000)).data
        rec.check(all(r["pr_number"] != 104 for r in before_unreachable["results"]),
                  "PR #104 (older history) is NOT retrievable before the backfill")

        # ── get_evidence expansion of the overflow thread ──
        rec.section("session 1: get_evidence expands the 53-comment overflow thread")
        cache = (await srv.call("semantic_search_reviews", query="cache TTL hit ratio staging replica key class",
                                repo=REPO, budget_tokens=16000)).data
        multi = [r for r in cache["results"] if r["part_count"] > 1]
        rec.check(multi, "the overflow thread is split into several parts (part_count > 1)")
        expanded = (await srv.call("get_evidence", evidence_id=multi[0]["evidence_id"], repo=REPO)).data
        parts = expanded["results"]
        rec.check(len(parts) == multi[0]["part_count"] and [p["part_index"] for p in parts] == list(range(len(parts))),
                  f"get_evidence returns all {len(parts)} parts in order")
        joined = "\n".join(p["excerpt"] for p in parts)
        rec.check("Comment 1:" in joined and "Comment 50:" in joined and "Comment 53:" in joined,
                  "expansion includes comment 50 (first page) and comment 53 (follow-up node query)")
        rec.check(all(not p["thread_incomplete"] for p in parts) and expanded["coverage"]["nested_incomplete_units"] == 0,
                  "nested pagination completed: no thread is flagged incomplete")
        node_queries = [e for e in sb.stub_entries() if e.get("op") == "GetThreadComments"]
        rec.check(node_queries, "the server issued a follow-up GetThreadComments node query")

        # ── temporary storage index (must vanish after restart) ──
        rec.section("session 1: temporary-storage index")
        await srv.call("ensure_repo_ready", repo=SCRATCH_REPO, storage="temporary", pages=1)
        tdone = await srv.poll_job(SCRATCH_REPO)
        rec.check(tdone.data["index_state"] in READY, f"temporary index is ready ({tdone.data['index_state']})")
        listed = (await srv.call("list_indexed_repos")).text
        rec.check(SCRATCH_REPO in listed and REPO in listed and "mem" in listed, "list_indexed_repos shows the permanent and the temporary repo")
        tsearch = (await srv.call("semantic_search_reviews", query="retry timeout", repo=SCRATCH_REPO, budget_tokens=4000)).data
        rec.check(tsearch["results"], "temporary index serves search")

        # ── first backfill page ──
        rec.section("session 1: backfill reaches older history (page 2)")
        t0 = time.monotonic()
        await srv.call("ensure_repo_ready", repo=REPO, backfill=True, pages=1)
        bdone = await srv.poll_job(REPO)
        rec.timings["backfill_page_secs"] = round(time.monotonic() - t0, 2)
        cov = bdone.data["coverage"]
        rec.check(bdone.data["index_state"] == "ready_partial", f"partial backfill -> ready_partial (got {bdone.data['index_state']})")
        rec.check(cov["pull_requests_indexed"] == 4 and cov["pr_number_min"] == 103 and cov["older_history_remaining"] is True,
                  f"coverage now spans #103-#106, older history still remaining ({cov['summary'][:90]})")
        rec.check(bdone.data["generation"] > gen_initial, "backfill published a new generation")
        after = (await srv.call("semantic_search_reviews", query="tombstone record when invalidating a cache key",
                                repo=REPO, budget_tokens=16000)).data
        rec.check(any(r["pr_number"] == 104 and "tombstone" in r["excerpt"] for r in after["results"]),
                  "PR #104 is now retrievable (previously unreachable)")
        gen_backfilled = bdone.data["generation"]

        # ── plain refresh ──
        rec.section("session 1: incremental refresh")
        await srv.call("ensure_repo_ready", repo=REPO, refresh=True, pages=1)
        rdone = await srv.poll_job(REPO)
        rec.check(rdone.data["index_state"] in READY, f"refresh succeeds ({rdone.data['index_state']})")
        rec.check(rdone.data["coverage"]["pull_requests_indexed"] == 4 and rdone.data["coverage"]["older_history_remaining"] is True,
                  "refresh keeps coverage (4 PRs, older history still remaining)")
        gen_before_revoke = rdone.data["generation"]
        docs_before_revoke = rdone.data["total_documents"]

        # ── revoked authorization ──
        rec.section("session 1: revoked authorization -> stale, previous generation still served")
        sb.revoke_flag.write_text("revoked")
        await srv.call("ensure_repo_ready", repo=REPO, refresh=True, pages=1)
        sdone = await srv.poll_job(REPO)
        sd = sdone.data
        rec.check(sd["index_state"] == "stale", f"revoked GraphQL auth -> index_state stale (got {sd['index_state']})")
        rec.check(sd["last_job"]["error_kind"] in {"authorization_revoked", "authorization_required"},
                  f"error_kind is {sd['last_job']['error_kind']}")
        rec.check(sd["stale_reason"] and sd["action"], "stale_reason and an action are given")
        rec.check(sd["generation"] == gen_before_revoke and sd["total_documents"] == docs_before_revoke,
                  "the previous generation is untouched by the failed refresh")
        senv = (await srv.call("semantic_search_reviews", query=q, repo=REPO, budget_tokens=16000)).data
        rec.check(senv["index_state"] == "stale" and senv["results"] and senv["generation"] == gen_before_revoke,
                  "search still serves the previous generation while stale")
        rec.check(any("stale" in w.lower() for w in senv["warnings"]) and senv.get("stale_reason"),
                  "stale search carries a warning and stale_reason (not an empty answer)")
        rec.facts["stale_search_warnings"] = _trim(senv["warnings"], 200)

        # ── kill ──
        rec.section("kill -9 the server")
        pid = srv.kill()
        rec.say(f"SIGKILLed server pid {pid}")
        reg = sb.registry(REPO)
        rec.facts["registry_before_restart"] = {k: reg[k] for k in (
            "active_generation", "backfill_cursor", "history_complete", "last_job_status", "last_job_error_kind")}
        rec.check(reg["backfill_cursor"] == "c:4" and reg["history_complete"] == 0,
                  f"SQLite registry holds backfill cursor c:4 (got {reg['backfill_cursor']!r})")
        rec.check(sb.registry(SCRATCH_REPO, "temporary") is None, "no temporary registry row was ever written to disk")

    # ───────────────────────── session 2 ─────────────────────────
    sb.revoke_flag.unlink()
    rec.section("session 2: new server process, same HOME")
    log_mark = len(sb.stub_entries())
    async with open_server(info, sb, rec) as srv2:
        pids = {int(sb.pid_file.read_text())}
        rec.check(pid not in pids, "a brand-new server process is running")

        status = (await srv2.call("get_github_connection_status")).data
        rec.check(status["connected"] is True and status["login"] == "demo-reviewer",
                  "credential persisted: connection status is connected after restart")

        st = (await srv2.call("get_index_stats", repo=REPO)).data
        rec.check(st["generation"] == gen_before_revoke and st["total_documents"] == docs_before_revoke
                  and st["coverage"]["pull_requests_indexed"] == 4,
                  "permanent index + coverage persisted (same generation, doc count, 4 PRs)")
        rec.check(st["coverage"]["older_history_remaining"] is True and st["coverage"]["history_complete"] is False,
                  "coverage still says older history remains")
        rec.check(st["index_state"] == "stale" and st["last_job"]["error_kind"] in {"authorization_revoked", "authorization_required"},
                  f"the failed-refresh outcome persisted too (state {st['index_state']})")
        env2 = (await srv2.call("semantic_search_reviews", query=q, repo=REPO, budget_tokens=16000)).data
        rec.check(env2["results"] and {"human_review_thread", "bot_review_thread"} <= _kinds(env2),
                  "search works in the new process (model reloaded from the local cache)")

        # temporary index is gone
        listed2 = (await srv2.call("list_indexed_repos")).text
        rec.check(REPO in listed2 and SCRATCH_REPO not in listed2, "temporary-storage index is gone after restart")
        tgone = (await srv2.call("semantic_search_reviews", query="retry timeout", repo=SCRATCH_REPO)).data
        rec.check(tgone["results"] == [] and tgone["index_state"] == "no_index",
                  "searching the temporary repo now returns an empty no_index envelope")

        # backfill resumes from the persisted cursor
        rec.section("session 2: backfill resumes from the persisted cursor")
        t0 = time.monotonic()
        await srv2.call("ensure_repo_ready", repo=REPO, backfill=True, pages=1)
        fdone = await srv2.poll_job(REPO)
        rec.timings["backfill_after_restart_secs"] = round(time.monotonic() - t0, 2)
        fd = fdone.data
        fetches = [e for e in sb.stub_entries()[log_mark:] if e.get("op") == "GetPRs" and e.get("repo") == REPO]
        rec.check(fetches and fetches[0]["order"] == "CREATED_AT" and fetches[0]["cursor"] == "c:4",
                  f"first GraphQL list request after restart resumed from cursor c:4 ({fetches[:1]})")
        # A failed refresh stays visible until a refresh succeeds: the backfill alone may not hide it.
        rec.check(fd["index_state"] in {"stale", "ready_selected_scope"},
                  f"backfill finished; state is {fd['index_state']}")
        if fd["index_state"] == "stale":
            rec.check("refresh" in (fd["stale_reason"] or "").lower(),
                      f"the lingering stale_reason names the failed refresh ({(fd['stale_reason'] or '')[:90]})")
        fcov = fd["coverage"]
        rec.check(fcov["history_complete"] is True and fcov["older_history_remaining"] is False
                  and fcov["pull_requests_indexed"] == 6 and fcov["pr_number_min"] == 101,
                  f"full closed history imported ({fcov['summary'][:100]})")
        rec.check(fd["generation"] > gen_backfilled, "a new generation was published")
        old = (await srv2.call("semantic_search_reviews", query="advisory lock before running migrations deploys race",
                               repo=REPO, budget_tokens=16000)).data
        rec.check(any(r["pr_number"] == 101 and "advisory lock" in r["excerpt"] for r in old["results"]),
                  "PR #101 (oldest) is retrievable after the post-restart backfill")
        rec.check(old["index_state"] in {"stale", "ready_selected_scope"} and old["coverage"]["older_history_remaining"] is False
                  and not any("older pull request history is not indexed" in w.lower() for w in old["warnings"]),
                  "no 'older history' warning once history is complete")
        again = await srv2.call("ensure_repo_ready", repo=REPO, backfill=True, pages=1)
        rec.check("Nothing to backfill" in again.text, "a further backfill reports nothing to do")

        # Auth is healthy again: a successful refresh clears the stale state.
        await srv2.call("ensure_repo_ready", repo=REPO, refresh=True, pages=1)
        healed = (await srv2.poll_job(REPO)).data
        rec.check(healed["index_state"] == "ready_selected_scope" and healed["stale_reason"] is None,
                  f"a successful refresh after re-auth returns to ready_selected_scope (got {healed['index_state']})")
        rec.check(healed["coverage"]["history_complete"] is True and healed["coverage"]["pull_requests_indexed"] == 6,
                  "refresh keeps the complete 6-PR history")
        final = (await srv2.call("semantic_search_reviews", query=q, repo=REPO, budget_tokens=16000)).data
        rec.check(final["index_state"] == "ready_selected_scope" and final["results"]
                  and not any("stale" in w.lower() for w in final["warnings"]),
                  "search is clean again: ready, results, no stale warning")

    # The product's startup update check (requests -> raw.githubusercontent.com) is refused
    # by the shim; it is the only outbound call besides the stubbed GitHub endpoints.
    refused = [e for e in sb.stub_entries() if e.get("unexpected")]
    update_checks = [e for e in refused if e.get("host") == "raw.githubusercontent.com"]
    unexpected = [e for e in refused if e not in update_checks]
    rec.facts["update_check_attempts_blocked"] = len(update_checks)
    rec.check(not unexpected, f"no unexpected outbound requests reached the shim ({unexpected[:2]})")
    rec.facts["graphql_requests"] = len([e for e in sb.stub_entries() if e.get("path") == "/graphql"])


def run_all(workdir: Path, rec: Recorder, installed: dict | None = None) -> dict:
    """Fresh install (unless ``installed`` is given) then the full flow; returns facts."""
    t0 = time.monotonic()
    info = installed or fresh_install(workdir, rec)
    encoder = choose_encoder()
    sb = Sandbox(workdir / "sandbox", encoder)
    rec.facts["encoder"] = (
        "real sentence-transformers all-MiniLM-L6-v2 from the local Hugging Face cache (HF_HUB_OFFLINE=1)"
        if encoder == "real" else "FAKE deterministic hashing encoder (MiniLM not cached)"
    )
    rec.say(f"encoder: {rec.facts['encoder']}")
    started = time.monotonic()
    try:
        asyncio.run(run_flow(info, sb, rec))
    except _EXCEPTION_GROUP as group:  # anyio task groups wrap the real failure
        leaf = group
        while _EXCEPTION_GROUP and isinstance(leaf, _EXCEPTION_GROUP):
            leaf = leaf.exceptions[0]
        raise leaf from None
    rec.timings["flow_secs"] = round(time.monotonic() - started, 2)
    rec.timings["total_secs"] = round(time.monotonic() - t0, 2)
    fakes = [e for e in sb.stub_entries() if e.get("event") == "fake_encoder_installed"]
    rec.facts["server_confirmed_fake_encoder"] = bool(fakes)
    if encoder == "real" and fakes:
        raise AssertionError("expected the real encoder but the shim installed the fake one")
    return rec.facts
