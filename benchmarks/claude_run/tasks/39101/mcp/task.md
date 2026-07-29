Repository: langchain-ai/langchain

Issue to resolve:
fix(anthropic): strip unsupported fields from system message content blocks

Fixes #39100

## What

`_format_messages` forwarded `SystemMessage` list-content blocks verbatim, so v1 content blocks built with `create_text_block()` leaked the LangChain-internal `id` field into the `system` payload. Anthropic rejects it with `400 system.0.id: Extra inputs are not permitted`. `get_num_tokens_from_messages` failed the same way since it shares `_format_messages`.

Human/assistant text blocks were already sanitized (keep `type`, `text`, `cache_control`, `citations`; drop null citation `file_id`). This extracts that narrowing into a `_format_text_block()` helper and applies it to system text blocks too. Non-text system blocks pass through unchanged.

## Tests

- `test__format_messages_system_v1_content_blocks_drop_id`: `create_text_block()`-built system message no longer leaks `id` (fails on main)
- `test__format_messages_system_text_block_preserves_supported_fields`: `cache_control` preserved, `id` stripped

Full unit suite: 241 passed. `ruff check` and `ruff format --check` clean.

Credit to @jmbledsoe for the report in #39100.

The change must be confined to these files:
  - libs/partners/anthropic/langchain_anthropic/chat_models.py
  - libs/partners/anthropic/tests/unit_tests/test_chat_models.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.