### From PR #38743
PR #38743: fix(fireworks): await `response.text()` in async error handling
Closes #38741

---

`Fireworks._acall` (the async completions path) built its HTTP error messages with `{response.text}`. On an `aiohttp` response, `.text` is a coroutine method, not a property — unlike a `requests` response, where it is a property — so it was never awaited and the raised error embedded a bound-method `repr()` instead of the API's response body. Any async error (`ainvoke` / `agenerate` hitting a 4xx/5xx) surfaced `<bound method ClientResponse.text ...>` instead of the reason the request failed. The synchronous `_call` path (which uses `requests`) already handled this correctly, and the async method already translated `requests`' `.status_code` to aiohttp's `.status`, so this was an oversight rather than intent.

This awaits the body in both error branches, matching the sync path.

A regression unit test is added (the async error path previously had no unit coverage); it fails on the prior behavior — asserting the message contained a coroutine `repr()` — and passes with the fix.

## Release note

Fixed `Fireworks` async calls (`ainvoke` / `agenerate`) so API error messages include the actual response body instead of a coroutine `repr()`.

---

*Disclaimer: this contribution was prepared with the assistance of an AI agent.*

[Extraction note: GitHub paginated only part of this PR's pullReq

### From PR #38743
PR #38743 Commit: fix(fireworks): await response.text() in async error handling

`Fireworks._acall` built its HTTP error messages with `{response.text}`, but
on an aiohttp response `.text` is a coroutine method, not a property (as it is
on a requests response). The messages therefore embedded a bound-method repr
instead of the API error body on any async 4xx/5xx. Await the body in both
error branches, matching the sync `_call` path. Adds a regression unit test.

Closes #38741

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38742
PR #38742 Commit: implement in langchain-anthropic, langchain-fireworks, langchain-openai
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38742
PR #38742: feat(anthropic,fireworks,openai): support langsmith gateway through env var
Enables [LLM Gateway](https://docs.langchain.com/langsmith/llm-gateway) for supported chat models:
```
LANGSMITH_GATEWAY=true
LANGSMITH_GATEWAY_API_KEY=...
```
or, using a custom URL:
```
LANGSMITH_GATEWAY=https://...
LANGSMITH_GATEWAY_API_KEY=...
```
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #36867
PR #36867: fix(core): unxfail stream error callback test
Fixes #36866

Remove the outdated `xfail` from `test_stream_error_callback` and update the empty stream error expectation to match the current callback payload shape. In the `i == 0` case, `LLMResult.generations` preserves the batch dimension, so the observed value is `[[]]` rather than `[]`.

Verified with:
- `uv run --python 3.11 --directory libs/core --group test python -m pytest tests/unit_tests/language_models/chat_models/test_base.py -k test_stream_error_callback -q -vv`
- `uv run --python 3.11 --directory libs/core --group test python -m pytest tests/unit_tests/language_models/chat_models/test_base.py -q`

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

### From PR #32496
PR #32496 | File: libs/core/langchain_core/v1/utils.py | Line: 1
Reviewer (mishra-krishna): This is a fantastic addition! These message display utilities will be incredibly helpful for debugging and understanding the content of messages. The different formatting options and Jupyter integration are excellent features. Great work!
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

### From PR #38742
PR #38742 overall review by open-swe [COMMENTED]: **Open SWE Review** found 1 potential issue.

[Open in Web](https://openswe.vercel.app/agents/reviews/langchain-ai/langchain/38742) • [View Open SWE trace](https://smith.langchain.com/o/ebbaf2eb-769b-4505-aca2-d11de10372a4/projects/p/f2e68aea-26b3-4a6a-9c4a-d04e7ebe2690/t/94e7c867-89c2-5085-8932-5d3f1f234f4b)

<!-- open-swe-reviewer pr=38742 -->
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38742
PR #38742 overall review by open-swe [COMMENTED]: **Open SWE Review** found 1 potential issue.

[Open in Web](https://openswe.vercel.app/agents/reviews/langchain-ai/langchain/38742) • [View Open SWE trace](https://smith.langchain.com/o/ebbaf2eb-769b-4505-aca2-d11de10372a4/projects/p/f2e68aea-26b3-4a6a-9c4a-d04e7ebe2690/t/94e7c867-89c2-5085-8932-5d3f1f234f4b)

<!-- open-swe-reviewer pr=38742 -->
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]