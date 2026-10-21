"""Hermetic shim loaded into the server subprocess (via PYTHONPATH) for the e2e test.

Python imports ``sitecustomize`` at interpreter start-up, so the product code
runs unmodified while every outbound HTTP call is answered locally:

* ``httpx`` clients get an ``httpx.MockTransport`` that fakes GitHub Device Flow
  (``github.com/login/...``), ``api.github.com/user`` and ``api.github.com/graphql``
  from the invented fixture in ``e2e_fixture.py``. Unknown hosts get a 404 and are
  logged as ``unexpected``.
* ``requests`` (used only by the telemetry / update check) is refused.
* If ``E2E_ENCODER=fake`` the SentenceTransformer in ``storage.encoder`` is swapped
  for a deterministic hashing encoder; otherwise the real cached MiniLM is used.

Behaviour switches (all through environment variables set by the harness):

``E2E_STUB``         must be "1" or this module does nothing.
``E2E_REVOKE_FLAG``  while this file exists GraphQL answers 401 (revoked auth).
``E2E_STUB_LOG``     JSON-lines request log (method, host, operation, cursor, status).
``E2E_PID_FILE``     the server writes its pid here so the harness can SIGKILL it.
"""

import hashlib
import importlib.abc
import json
import os
import sys
import time
import urllib.parse

if os.environ.get("E2E_STUB") == "1":
    import httpx

    import e2e_fixture as fx

    _LOG = os.environ.get("E2E_STUB_LOG")
    _REVOKE_FLAG = os.environ.get("E2E_REVOKE_FLAG")
    ACCESS_TOKEN = "e2e-access-token-not-a-real-secret"
    _state = {"device_polls": 0, "last_repo": "demo-org/demo-repo"}
    _repo_cache: dict = {}

    def _log(**fields):
        if not _LOG:
            return
        fields["t"] = round(time.time(), 3)
        fields["pid"] = os.getpid()
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(fields) + "\n")

    def _json(status, payload, headers=None):
        return httpx.Response(status, json=payload, headers=headers or {})

    def _form(request):
        return {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}

    def _prs_for(repo):
        return _repo_cache.setdefault(repo, fx.build_prs(repo))

    def _graphql(request):
        auth = request.headers.get("authorization", "")
        revoked = bool(_REVOKE_FLAG and os.path.exists(_REVOKE_FLAG))
        body = json.loads(request.content.decode() or "{}")
        query = body.get("query", "")
        variables = body.get("variables", {}) or {}
        if "GetThreadComments" in query:
            op = "GetThreadComments"
        elif "GetPRReviewThreads" in query:
            op = "GetPRReviewThreads"
        else:
            op = "GetPRs"
        order = "CREATED_AT" if "field: CREATED_AT" in query else "UPDATED_AT"
        entry = {
            "op": op,
            "order": order if op == "GetPRs" else None,
            "cursor": variables.get("cursor"),
            "repo": f"{variables.get('owner')}/{variables.get('repo')}" if op == "GetPRs" else None,
        }
        if revoked or auth != f"Bearer {ACCESS_TOKEN}":
            _log(method="POST", host="api.github.com", path="/graphql", status=401,
                 revoked=revoked, **entry)
            return _json(401, {"message": "Bad credentials"})
        if op == "GetPRs":
            repo = f"{variables.get('owner')}/{variables.get('repo')}"
            _state["last_repo"] = repo
            payload = fx.pr_page(_prs_for(repo), order, variables.get("cursor"))
        else:
            # Node ids carry no repo; follow-ups belong to the repo of the last list query.
            prs = _prs_for(_state["last_repo"])
            if op == "GetThreadComments":
                payload = fx.thread_comments_page(prs, variables.get("id"), variables.get("cursor"))
            else:
                payload = fx.pr_threads_page(prs, variables.get("id"), variables.get("cursor"))
        _log(method="POST", host="api.github.com", path="/graphql", status=200, **entry)
        return _json(200, payload, {"X-RateLimit-Remaining": "4999"})

    def _handler(request):
        url = request.url
        host, path = url.host, url.path
        if host == "github.com" and path == "/login/device/code":
            _log(method="POST", host=host, path=path, status=200)
            return _json(200, {
                "device_code": "e2e-device-code-not-a-real-secret",
                "user_code": "E2E1-CODE",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 1,
            })
        if host == "github.com" and path == "/login/oauth/access_token":
            form = _form(request)
            if form.get("grant_type") == "refresh_token":
                _log(method="POST", host=host, path=path, status=200, grant="refresh_token")
                return _json(200, {"access_token": ACCESS_TOKEN, "refresh_token": "e2e-refresh-2",
                                   "expires_in": 28800, "refresh_token_expires_in": 15897600})
            _state["device_polls"] += 1
            if _state["device_polls"] == 1:
                _log(method="POST", host=host, path=path, status=200, grant="device_code",
                     result="authorization_pending")
                return _json(200, {"error": "authorization_pending"})
            _log(method="POST", host=host, path=path, status=200, grant="device_code",
                 result="granted")
            return _json(200, {"access_token": ACCESS_TOKEN, "token_type": "bearer",
                               "expires_in": 28800, "refresh_token": "e2e-refresh-1",
                               "refresh_token_expires_in": 15897600})
        if host == "api.github.com" and path == "/user":
            ok = request.headers.get("authorization") == f"Bearer {ACCESS_TOKEN}"
            _log(method="GET", host=host, path=path, status=200 if ok else 401)
            if not ok:
                return _json(401, {"message": "Bad credentials"})
            return _json(200, {"login": "demo-reviewer", "id": 424242})
        if host == "api.github.com" and path == "/graphql":
            return _graphql(request)
        _log(method=request.method, host=host, path=path, status=404, unexpected=True)
        return _json(404, {"message": "blocked by the e2e hermetic shim"})

    _MOCK = httpx.MockTransport(_handler)

    def _wrap_init(cls):
        original = cls.__init__

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", _MOCK)
            original(self, *args, **kwargs)

        cls.__init__ = __init__

    _wrap_init(httpx.AsyncClient)
    _wrap_init(httpx.Client)

    try:  # the telemetry / update check uses requests: refuse it entirely
        import requests.adapters

        def _refuse(self, request, **kwargs):
            _log(method=request.method, host=urllib.parse.urlparse(request.url).netloc,
                 path="(requests)", status=0, unexpected=True, blocked=True)
            raise requests.exceptions.ConnectionError("blocked by the e2e hermetic shim")

        requests.adapters.HTTPAdapter.send = _refuse
    except ImportError:
        pass

    # ── optional deterministic encoder instead of the real MiniLM ────────────────
    class _FakeModel:
        """Hashing bag-of-words embedder (384-d, L2-normalised); same API shape."""

        DIM = 384

        def _vec(self, text):
            import numpy as np

            vec = np.zeros(self.DIM, dtype="float32")
            for token in "".join(c.lower() if c.isalnum() else " " for c in text).split():
                digest = hashlib.md5(token.encode()).digest()
                vec[int.from_bytes(digest[:4], "little") % self.DIM] += 1.0
            norm = float(np.linalg.norm(vec)) or 1.0
            return vec / norm

        def encode(self, texts, *args, **kwargs):
            import numpy as np

            if isinstance(texts, str):
                return self._vec(texts)
            return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.DIM))

    _FAKE = _FakeModel()

    def _patch_encoder(module):
        module._get_model = lambda: _FAKE
        _log(event="fake_encoder_installed")

    class _EncoderFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name != "storage.encoder":
                return None
            sys.meta_path.remove(self)
            try:
                import importlib.util

                spec = importlib.util.find_spec(name)
            finally:
                sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return None
            loader = spec.loader
            original_exec = loader.exec_module

            def exec_module(module):
                original_exec(module)
                _patch_encoder(module)

            loader.exec_module = exec_module
            return spec

    if os.environ.get("E2E_ENCODER") == "fake":
        sys.meta_path.insert(0, _EncoderFinder())

    _pid_file = os.environ.get("E2E_PID_FILE")
    if _pid_file and os.path.basename(sys.argv[0] or "") == "github-pr-context-mcp":
        with open(_pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        _log(event="server_start")
