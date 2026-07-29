### From PR #32072
PR #32072 | File: libs/core/langchain_core/messages/content_blocks.py | Line: 108
Code Context:
@@ -83,6 +103,22 @@ class ToolCallContentBlock(TypedDict):
     """Tool call ID."""
 
 
+def make_tool_call_block(
+    tool_call_id: str,
+) -> dict[str, Any]:
Reviewer (sydney-runkle): ```suggestion
) -> ToolCallContentBlock:
```
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38672
PR #38672: fix(core): avoid inherited docstrings for tools
Fixes #32066

---

Class-based tools built from Pydantic models should only use a docstring that is defined directly on the tool class. Before this change, schema inference used `inspect.getdoc`, which can walk the MRO and pull in a parent model's docstring. That made a subclass without its own docstring appear to have the parent's description.

This updates tool schema description inference to read direct docstrings only, while preserving the existing behavior that Pydantic class tools without a docstring are allowed and receive an empty description. Regular functions without a docstring still raise the existing error.

## Release note

Class-based tools no longer inherit parent Pydantic model docstrings as their tool or schema description when the subclass does not define its own docstring.

Verified with `uv run pytest tests/unit_tests/test_tools.py -q`, `uv run ruff check langchain_core/tools/base.py langchain_core/tools/structured.py tests/unit_tests/test_tools.py`, and `uv run ruff format --check langchain_core/tools/base.py langchain_core/tools/structured.py tests/unit_tests/test_tools.py` from `libs/core`.

AI assistance was used to prepare this contribution; the diff and test results were reviewed before submission.
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

### From PR #34516
PR #34516 Commit: deepseek: preserve reasoning_content in multi-turn conversations

Fixes #34166

When using `deepseek-reasoner` with tool calling, the second API call
fails with "Missing `reasoning_content` field in the assistant message".
This occurs because the parent's `_get_request_payload` method doesn't
preserve the `reasoning_content` field from `additional_kwargs` when
converting messages to the payload format.

Changes:
- Modified `_get_request_payload` to extract `reasoning_content` from
  `AIMessage.additional_kwargs` before calling the parent method
- Added logic to include `reasoning_content` in the assistant message
  payload for multi-turn conversations
- For `deepseek-reasoner` models, automatically add empty
  `reasoning_content` if not present (API requires this field)

This ensures that when resending conversation history to the DeepSeek
API, the `reasoning_content` field is properly included in assistant
messages, fixing the 400 error in multi-turn tool calling scenarios.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38609
PR #38609 Commit: fix(openai): preserve Pydantic schema for structured output on Responses API

When `reasoning` forces the Responses API path, `with_structured_output()`
converts the Pydantic class to a dict via `_convert_to_openai_response_format`,
losing the original class reference. This causes `_generate`/`_agenerate` to
call `responses.create()` instead of `responses.parse()`, so servers that don't
enforce structured output return plain text that fails Pydantic validation.

Fix: pass the original Pydantic class through a private `_pydantic_response_format`
kwarg so both `_construct_responses_api_payload` (sets `text_format`) and
`_generate`/`_agenerate` (chooses `responses.parse()`) can use it.

Also adds bidirectional `reasoning` dict → `reasoning_effort` translation on the
Chat Completions path, so `use_responses_api=False` works as an escape hatch
without raising `TypeError`.

Assisted-by: Claude Opus 4.6
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #34126
PR #34126 Commit: fix(openai): pass `strict` for `response_format` in `bind_tools()`
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #34516
PR #34516: fix(deepseek): preserve `reasoning_content` in multi-turn conversations
## Summary

Fixes #34166

When using `deepseek-reasoner` with tool calling, multi-turn conversations fail with:
```
Missing `reasoning_content` field in the assistant message at message index 1
```

**Root Cause:** The parent's `_get_request_payload` method doesn't preserve the `reasoning_content` field from `additional_kwargs` when converting `AIMessage` to the payload format.

## Changes

- Modified `_get_request_payload` to extract `reasoning_content` from `AIMessage.additional_kwargs` before calling the parent method
- Added logic to include `reasoning_content` in the assistant message payload for multi-turn conversations
- For `deepseek-reasoner` models, automatically add empty `reasoning_content` if not present (DeepSeek API requires this field)

## Test plan

Added 3 new unit tests:
- `test_get_request_payload_preserves_reasoning_content` - verifies reasoning_content is preserved from additional_kwargs
- `test_get_request_payload_adds_empty_reasoning_for_reasoner` - verifies empty reasoning_content is added for reasoner models
- `test_get_request_payload_no_reasoning_for_non_reasoner` - verifies non-reasoner models don't get reasoning_content added

## Disclaimer

This PR was generated with assistance from an AI agent (Claude Code).

🤖 Generated with [Claude Code](https://claude.com/claude

### From PR #38672
PR #38672 Commit: fix(core): avoid inherited docstrings for tools

AI-assisted contribution: OpenAI Codex was used to prepare this change; the diff and test results were reviewed before submission.

Signed-off-by: sanhuo11 <tlysanhuo@gmail.com>
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #34126
PR #34126: fix(openai): pass `strict` for `response_format` in `bind_tools()`
when passing `response_format`, we didn't use strict validation, leading to occasional omission of required fields

https://platform.openai.com/docs/guides/structured-outputs
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #32452
PR #32452 Commit: remove custom tool logic
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]