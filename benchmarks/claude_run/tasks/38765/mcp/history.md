### From PR #38625
PR #38625 Commit: fix(openai): prevent pydantic serializer warnings when streaming structured output

Applies the same parsed exclusion logic from #35543 to the streaming delta payload, preventing noisy UserWarning logs during with_structured_output streaming.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38625
PR #38625: fix(openai): prevent pydantic serializer warnings when streaming structured output
Closes #38034

In PR #35543, `parsed` was correctly excluded from `model_dump` on the standard chat completion path to prevent Pydantic serialization warnings when using `with_structured_output`. However, this exclusion was omitted in the `_stream` and `_astream` generators, causing massive terminal spam (`UserWarning: Pydantic serializer warnings`) when users stream structured outputs.

This PR applies the identical exclusion logic (`exclude={"choices": {"__all__": {"delta": {"parsed"}}}}`) to the streaming chunk `model_dump()` calls, silently resolving the serialization warnings without altering the public API.

## Release note
Fixes Pydantic serialization warnings printed to the console when streaming with `with_structured_output`.

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

### From PR #38760
PR #38760 Commit: Fix custom header aggregation in MarkdownHeaderTextSplitter

aggregate_lines_to_chunks() checked whether the previous chunk's last
line started with '#' to decide if nested headers should be merged into
a single chunk.  This only worked for standard Markdown headers and
caused custom header patterns (e.g., **Header) to be split into
separate chunks even when strip_headers=False.

Add _match_header_sep() that returns the matching (sep, name) for a
line, delegate _is_header_line() to it, and use it in both
aggregate_lines_to_chunks() and split_text() so header detection is
not duplicated.

Add tests for strip_headers=False with custom and mixed headers to
cover the exact scenario where the old '#' check broke chunk boundaries.

Closes #38702.

Test Plan:

Signed-off-by: Functionhx <2994114386@qq.com>
Signed-off-by: Yuchen Fan <functionhx@gmail.com>
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38501
PR #38501 Commit: fix(core): ignore empty strings in `_extract_reasoning_from_additional_kwargs`

Some model providers (e.g., ChatTongyi) set `reasoning_content` to an
empty string after the reasoning stage completes. This creates empty
`ReasoningContentBlock` entries in `content_blocks` when
`LC_OUTPUT_VERSION=v1` is set.

Change the condition from `is not None` to a truthy check so that empty
strings are treated the same as `None`.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #34846
PR #34846 Commit: fix(core): fix for `exclude_inputs` `POST`
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #36867
PR #36867: fix(core): unxfail stream error callback test
Fixes #36866

Remove the outdated `xfail` from `test_stream_error_callback` and update the empty stream error expectation to match the current callback payload shape. In the `i == 0` case, `LLMResult.generations` preserves the batch dimension, so the observed value is `[[]]` rather than `[]`.

Verified with:
- `uv run --python 3.11 --directory libs/core --group test python -m pytest tests/unit_tests/language_models/chat_models/test_base.py -k test_stream_error_callback -q -vv`
- `uv run --python 3.11 --directory libs/core --group test python -m pytest tests/unit_tests/language_models/chat_models/test_base.py -q`

[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #32452
PR #32452 Commit: fix streaming case
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38758
PR #38758 Commit: fix(core): promote safe configurable scalars to metadata (#37373)
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]