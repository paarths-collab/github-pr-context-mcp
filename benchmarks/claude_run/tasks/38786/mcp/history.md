### From PR #35085
PR #35085 Commit: feat(langchain): add provider inference metadata to `init_chat_model`
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38755
PR #38755 | File: libs/partners/anthropic/tests/unit_tests/test_chat_models_auth.py | Line: 32
Code Context:
@@ -0,0 +1,80 @@
+import os
+import pytest
+import asyncio
+from unittest.mock import MagicMock, patch
+from langchain_anthropic import ChatAnthropic
+from langchain_core.messages import HumanMessage
+
+def test_chat_anthropic_callable_api_key() -> None:
+    mock_key = "mock-key-from-callable"
+    def get_key() -> str:
+        return mock_key
+    
+    llm = ChatAnthropic(api_key=get_key, model="claude-3-opus-20240229")
+    
+    with patch.object(llm._client.messages, "create") as mock_create:
+        mock_message = MagicMock()
+        mock_message.text = "response"
+        mock_message.type = "text"
+        mock_response = MagicMock(content=[mock_message])
+        mock_response.usage = MagicMock(input_tokens=10, output_tokens=10)
+        mock_response.id = "msg_123"
+        mock_response.model = "claude-3-opus-20240229"
+        mock_response.role = "assistant"
+        mock_create.return_value = mock_response
+        
+        llm.invoke("Hello")
+        mock_create.assert_called_once()
+        kwargs = mock_create.call_args.kwargs
+        assert "extra_headers" in kwargs
+        assert kwargs["extra_headers"]["x-api-key"] == mock_key
+
+def test_chat_anthropic_auth_token_provider() -> None:
Reviewer (copilot-pull-request-reviewer): PEP8 expects two b

### From PR #38755
PR #38755 | File: libs/partners/anthropic/tests/unit_tests/test_chat_models_auth.py | Line: 56
Code Context:
@@ -0,0 +1,80 @@
+import os
+import pytest
+import asyncio
+from unittest.mock import MagicMock, patch
+from langchain_anthropic import ChatAnthropic
+from langchain_core.messages import HumanMessage
+
+def test_chat_anthropic_callable_api_key() -> None:
+    mock_key = "mock-key-from-callable"
+    def get_key() -> str:
+        return mock_key
+    
+    llm = ChatAnthropic(api_key=get_key, model="claude-3-opus-20240229")
+    
+    with patch.object(llm._client.messages, "create") as mock_create:
+        mock_message = MagicMock()
+        mock_message.text = "response"
+        mock_message.type = "text"
+        mock_response = MagicMock(content=[mock_message])
+        mock_response.usage = MagicMock(input_tokens=10, output_tokens=10)
+        mock_response.id = "msg_123"
+        mock_response.model = "claude-3-opus-20240229"
+        mock_response.role = "assistant"
+        mock_create.return_value = mock_response
+        
+        llm.invoke("Hello")
+        mock_create.assert_called_once()
+        kwargs = mock_create.call_args.kwargs
+        assert "extra_headers" in kwargs
+        assert kwargs["extra_headers"]["x-api-key"] == mock_key
+
+def test_chat_anthropic_auth_token_provider() -> None:
+    mock_token = "mock-token"
+    def get_token() -> str:


### From PR #35085
PR #35085: feat(langchain): attach inference metadata to `init_chat_model` models
- currently, `init_chat_model("gpt-4o")` silently infers `"openai"` from the model name, but consumers have no way to detect this inference programmatically.
- We add two attributes to the returned `BaseChatModel`:
  - `provider_was_inferred: bool`: `True` when the provider was guessed from the model name rather than explicitly supplied.
  - `resolved_provider: str`: the normalized provider string (e.g., `'openai'`).
- Attributes are set via `object.__setattr__`. They are intentionally not Pydantic fields (more below)
- Not applicable to `_ConfigurableModel` (inference happens per-invocation).

### Why `object.__setattr__`

Alternatives considered: adding Pydantic fields to `BaseChatModel` in core (leaks factory concerns into every model instance), returning a wrapper/tuple (breaking API change).

`object.__setattr__` is the only approach that adds metadata without breaking any existing API surface or modifying `langchain-core`.

## Example use case

In `deepagents-cli`, if someone were to use `/model gpt-4o`, it is useful to print a message such as "`langchain-openai` provider selected" to give visibility in the underlying abstraction calling the model.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38755
PR #38755 | File: libs/partners/anthropic/langchain_anthropic/chat_models.py | Line: 1501
Code Context:
@@ -1427,12 +1447,71 @@ def _get_request_payload(
 
         return {k: v for k, v in payload.items() if v is not None}
 
+    def _resolve_auth_headers(self) -> dict[str, str]:
+        if self.auth_token_provider is not None:
+            token = self.auth_token_provider()
+            if inspect.isawaitable(token):
+                try:
+                    loop = asyncio.get_running_loop()
+                except RuntimeError:
+                    loop = None
+                if loop and loop.is_running():
+                    raise RuntimeError(
+                        "Cannot use async auth_token_provider in sync methods when an event loop is running. "
+                        "Use the async methods (e.g. ainvoke, astream) instead."
+                    )
+                token = asyncio.run(token)
+            return {"Authorization": f"Bearer {token}"}
+        elif self.anthropic_api_key is not None:
+            if isinstance(self.anthropic_api_key, SecretStr):
+                return {"x-api-key": self.anthropic_api_key.get_secret_value()}
+            elif isinstance(self.anthropic_api_key, str):
+                return {"x-api-key": self.anthropic_api_key}
+            else:
+                key = self.anthropic_api_key()
+                if inspect.isawaitable

### From PR #38755
PR #38755 | File: libs/partners/anthropic/langchain_anthropic/chat_models.py | Line: 1506
Code Context:
@@ -1427,12 +1447,71 @@ def _get_request_payload(
 
         return {k: v for k, v in payload.items() if v is not None}
 
+    def _resolve_auth_headers(self) -> dict[str, str]:
+        if self.auth_token_provider is not None:
+            token = self.auth_token_provider()
+            if inspect.isawaitable(token):
+                try:
+                    loop = asyncio.get_running_loop()
+                except RuntimeError:
+                    loop = None
+                if loop and loop.is_running():
+                    raise RuntimeError(
+                        "Cannot use async auth_token_provider in sync methods when an event loop is running. "
+                        "Use the async methods (e.g. ainvoke, astream) instead."
+                    )
+                token = asyncio.run(token)
+            return {"Authorization": f"Bearer {token}"}
+        elif self.anthropic_api_key is not None:
+            if isinstance(self.anthropic_api_key, SecretStr):
+                return {"x-api-key": self.anthropic_api_key.get_secret_value()}
+            elif isinstance(self.anthropic_api_key, str):
+                return {"x-api-key": self.anthropic_api_key}
+            else:
+                key = self.anthropic_api_key()
+                if inspect.isawaitable

### From PR #38755
PR #38755 | File: libs/partners/anthropic/langchain_anthropic/chat_models.py | Line: 1514
Code Context:
@@ -1427,12 +1447,71 @@ def _get_request_payload(
 
         return {k: v for k, v in payload.items() if v is not None}
 
+    def _resolve_auth_headers(self) -> dict[str, str]:
+        if self.auth_token_provider is not None:
+            token = self.auth_token_provider()
+            if inspect.isawaitable(token):
+                try:
+                    loop = asyncio.get_running_loop()
+                except RuntimeError:
+                    loop = None
+                if loop and loop.is_running():
+                    raise RuntimeError(
+                        "Cannot use async auth_token_provider in sync methods when an event loop is running. "
+                        "Use the async methods (e.g. ainvoke, astream) instead."
+                    )
+                token = asyncio.run(token)
+            return {"Authorization": f"Bearer {token}"}
+        elif self.anthropic_api_key is not None:
+            if isinstance(self.anthropic_api_key, SecretStr):
+                return {"x-api-key": self.anthropic_api_key.get_secret_value()}
+            elif isinstance(self.anthropic_api_key, str):
+                return {"x-api-key": self.anthropic_api_key}
+            else:
+                key = self.anthropic_api_key()
+                if inspect.isawaitable

### From PR #38755
PR #38755 | File: libs/partners/anthropic/tests/unit_tests/test_chat_models_auth.py | Line: 8
Code Context:
@@ -0,0 +1,80 @@
+import os
+import pytest
+import asyncio
+from unittest.mock import MagicMock, patch
+from langchain_anthropic import ChatAnthropic
+from langchain_core.messages import HumanMessage
+
+def test_chat_anthropic_callable_api_key() -> None:
Reviewer (copilot-pull-request-reviewer): This test module has unused imports (`os`, `HumanMessage`) and import grouping/order that will likely fail ruff/pyflakes/isort checks. Remove unused imports and group stdlib/third-party/local imports consistently.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38755
PR #38755 | File: libs/partners/anthropic/langchain_anthropic/chat_models.py | Line: 13
Code Context:
@@ -8,7 +8,9 @@
 import json
 import re
 import warnings
-from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
+import inspect
+import asyncio
+from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
Reviewer (copilot-pull-request-reviewer): Imports are no longer in the file’s existing sorted order (e.g., `warnings` now appears before `inspect`/`asyncio`). This will likely fail ruff/isort checks; reorder the stdlib imports to keep them alphabetized.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #34126
PR #34126 | File: libs/partners/openai/langchain_openai/chat_models/base.py | Line: 1890
Code Context:
@@ -1865,17 +1876,20 @@ def bind_tools(
             kwargs["tool_choice"] = tool_choice
 
         if response_format:
+            # response_format present when using agents.create_agent's ProviderStrategy
+            # ---
+            # ProviderStrategy converts to OpenAI-style format, uses
+            # response_format
             if (
                 isinstance(response_format, dict)
                 and response_format.get("type") == "json_schema"
                 and "schema" in response_format.get("json_schema", {})
             ):
-                # compat with langchain.agents.create_agent response_format, which is
-                # an approximation of OpenAI format
                 response_format = cast(dict, response_format["json_schema"]["schema"])
             kwargs["response_format"] = _convert_to_openai_response_format(
-                response_format
+                response_format, strict=strict
Reviewer (mdrxy): this is the change; the rest is formatting/nits
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]