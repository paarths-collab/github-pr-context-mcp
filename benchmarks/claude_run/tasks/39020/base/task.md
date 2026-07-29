Repository: langchain-ai/langchain

Issue to resolve:
fix(core): use `tool_call_schema` cache for `BaseTool` token counting in `count_tokens_approximately`

### Summary

`count_tokens_approximately(..., tools=...)` recomputes each `BaseTool`'s OpenAI schema on every call by going through `convert_to_openai_tool()`, even though `BaseTool` already caches an equivalent schema via `tool_call_schema`. For agents with many schema-rich tools, this becomes a significant per-turn cost (e.g. `SummarizationMiddleware` calls it every turn to decide when to compact history).

This PR reuses the cached `tool_call_schema` for `BaseTool` instances during token counting. Other tool types (dicts, callables, `BaseModel` classes) continue using the existing path unchanged.

### Benchmark
Average per-tool schema generation time when ran over 80 tools:

| Path | Cold (1st call) | Warm (subsequent calls) |
|------|----------------:|------------------------:|
| `convert_to_openai_tool()` | 0.0243 ms | 0.0234 ms |
| `tool.tool_call_schema.model_json_schema()` | 0.0005 ms | 0.0001 ms |

This is roughly a **50× speedup on cold calls** and over **200× on warm calls** for the schema generation step.

`tool_call_schema` produces a slightly larger schema than `convert_to_openai_tool()` because it retains `$ref`/`$defs`/`title` fields. Since `count_tokens_approximately` is already an estimate (used only for trigger decisions), this trades a small overestimation for a much cheaper computation. Also handles the case where `tool_call_schema` is already a raw dict.

The change must be confined to these files:
  - libs/core/langchain_core/messages/utils.py
  - libs/core/tests/unit_tests/messages/test_utils.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.