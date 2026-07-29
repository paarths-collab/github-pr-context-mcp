Repository: langchain-ai/langchain

Issue to resolve:
fix(anthropic): add advisor_ prefix to builtin tool recognition

Fixes #38644

`ChatAnthropic.bind_tools()` crashes with `KeyError: 'parameters'` when an `advisor_`-prefixed Anthropic built-in tool (e.g. `advisor_20260301`) is passed, because the tool is routed through `convert_to_anthropic_tool()` which expects a generic function schema. Adding `"advisor_"` to `_BUILTIN_TOOL_PREFIXES` makes `_is_builtin_tool()` return `True` so the tool dict is passed through unchanged — the same path already used for `web_search_`, `computer_`, `memory_`, etc.

## Release note

`ChatAnthropic.bind_tools()` now correctly handles Anthropic provider-native tools with the `advisor_` prefix without raising `KeyError: 'parameters'`.

The change must be confined to these files:
  - libs/partners/anthropic/langchain_anthropic/chat_models.py
  - libs/partners/anthropic/tests/unit_tests/test_chat_models.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.