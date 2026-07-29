### From PR #38771
PR #38771 Commit: fix(core): unwrap RootModel[TypedDict] in convert_runnable_to_tool

Fixes #38713

When exposing a compiled StateGraph with TypedDict input as a tool via
convert_runnable_to_tool, the OpenAI tool schema advertised a nested
"root" property that didn't match what tool.invoke() accepted,
causing validation errors for LLM tool calls.

Root cause: get_input_schema() returns RootModel[TypedDict] for
StateGraph with TypedDict input. The $ref in its JSON schema bypassed
the type=="object" branch, falling through to
_get_schema_from_runnable_and_arg_types which used InputType=Any and
produced an empty model.

Fix: Before the existing schema-branch logic, detect RootModel wrapping
a TypedDict and unwrap it into a flat BaseModel with the TypedDict's
fields as top-level fields. This runs only when no explicit args_schema
was provided, preserving existing behavior for custom schemas and
arg_types.

Changes:
- libs/core/langchain_core/tools/convert.py: Add RootModel/is_typeddict
  imports and RootModel[TypedDict] unwrapping logic
- libs/core/tests/unit_tests/test_tools.py: Add regression test verifying
  flat schema shape, no nested root property, and flat dict invocation
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38772
PR #38772 Commit: fix(core): unwrap RootModel[TypedDict] in convert_runnable_to_tool

Fixes #38713

When exposing a compiled StateGraph with TypedDict input as a tool via
convert_runnable_to_tool, the OpenAI tool schema advertised a nested
"root" property that didn't match what tool.invoke() accepted,
causing validation errors for LLM tool calls.

Root cause: get_input_schema() returns RootModel[TypedDict] for
StateGraph with TypedDict input. The $ref in its JSON schema bypassed
the type=="object" branch, falling through to
_get_schema_from_runnable_and_arg_types which used InputType=Any and
produced an empty model.

Fix: Before the existing schema-branch logic, detect RootModel wrapping
a TypedDict and unwrap it into a flat BaseModel with the TypedDict's
fields as top-level fields. This runs only when no explicit args_schema
was provided, preserving existing behavior for custom schemas and
arg_types.

Changes:
- libs/core/langchain_core/tools/convert.py: Add RootModel/is_typeddict
  imports and RootModel[TypedDict] unwrapping logic
- libs/core/tests/unit_tests/test_tools.py: Add regression test verifying
  flat schema shape, no nested root property, and flat dict invocation
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38762
PR #38762 | File: libs/partners/openai/langchain_openai/chat_models/base.py | Line: None
Code Context:
@@ -4056,11 +4102,13 @@ def _create_usage_metadata(
     if service_tier is not None:
         # Avoid counting cache and reasoning tokens towards the service tier token
         # counts, since service tier tokens are already priced differently
-        input_token_details[service_tier] = input_tokens - input_token_details.get(
-            f"{service_tier_prefix}cache_read", 0
+        input_token_details[service_tier] = (
+            input_tokens
+            - (input_token_details.get(f"{service_tier_prefix}cache_read", 0) or 0)
+            - (input_token_details.get(f"{service_tier_prefix}cache_creation", 0) or 0)
         )
Reviewer (open-swe): Fixed in `92c178c`: both usage-metadata paths now keep `cache_write_tokens` as a separate cache-creation detail without subtracting it from the priority/flex input count, and the new overlapping-breakpoint tests cover the negative-count case.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38770
PR #38770: fix(core): flatten RootModel TypedDict tool schemas
## Summary

Fixes #38713.

`convert_runnable_to_tool` currently trusts `runnable.input_schema` whenever `runnable.get_input_jsonschema()` looks like a normal object schema. That breaks for runnables whose public JSON schema is flat but whose Pydantic input schema is a `RootModel[TypedDict]`, such as a compiled LangGraph exposed through `Runnable.as_tool(...)`.

In that case the exported OpenAI tool schema can advertise a nested `root` property, while `tool.invoke(...)` still expects the TypedDict fields at the top level. This PR detects the RootModel-wrapped TypedDict case and derives the tool args from the runnable input type instead, preserving the flat public schema shape.

## Current state

This PR was automatically closed by the assignment gate because I am not assigned to the linked issue yet. I posted the detailed approach and validation on #38713 and will wait for maintainer direction before reopening. Corrected fork branch head: `dc6e0cd2245363d9e96be0f0dd012f98ec831f24`.

## Tests

- `uv run --directory libs/core --group test pytest tests/unit_tests/test_tools.py::test_convert_runnable_to_tool_unwraps_root_model_typed_dict -q`
- `uv run --directory libs/core --group test pytest tests/unit_tests/test_tools.py -q` -> 180 passed, 7 skipped
- `uv run --directory libs/core --all-groups mypy tests/unit_tests/tes

### From PR #38772
PR #38772: fix(core): unwrap RootModel[TypedDict] in convert_runnable_to_tool
Closes #38713

---

## Problem

When exposing a compiled `StateGraph` (with `TypedDict` input) as a tool via `convert_runnable_to_tool`, the OpenAI tool schema advertised a nested `"root"` property that didn't match what `tool.invoke()` accepted, causing validation errors for LLM tool calls.

The root cause is that `StateGraph.get_input_schema()` returns `RootModel[TypedDict]` (via `create_model_v2(root=TypedDict)`). The `$ref` in its JSON schema bypasses the `type == "object"` branch in `convert_runnable_to_tool`, falling through to `_get_schema_from_runnable_and_arg_types` which uses `InputType = Any` and produces an empty model.

## What Changed

### `libs/core/langchain_core/tools/convert.py`

Added imports for `RootModel` (from pydantic) and `is_typeddict` (from typing_extensions).

Before the existing schema-branch logic in `convert_runnable_to_tool`, added a check that detects when `runnable.input_schema` is a `RootModel` wrapping a `TypedDict`. When detected, the `TypedDict` fields are extracted and used to create a flat `BaseModel` via `create_model()`, with the tool name as the model title. This runs only when no explicit `args_schema` was provided by the caller, so existing behavior for custom schemas and `arg_types` is fully preserved.

### `libs/core/tests/unit_tests/test_tools.py`

Added

### From PR #38771
PR #38771: fix(core): unwrap RootModel[TypedDict] in convert_runnable_to_tool
Closes #38713

---

## Problem

When exposing a compiled `StateGraph` (with `TypedDict` input) as a tool via `convert_runnable_to_tool`, the OpenAI tool schema advertised a nested `"root"` property that didn't match what `tool.invoke()` accepted, causing validation errors for LLM tool calls.

The root cause is that `StateGraph.get_input_schema()` returns `RootModel[TypedDict]` (via `create_model_v2(root=TypedDict)`). The `$ref` in its JSON schema bypasses the `type == "object"` branch in `convert_runnable_to_tool`, falling through to `_get_schema_from_runnable_and_arg_types` which uses `InputType = Any` and produces an empty model.

## What Changed

### `libs/core/langchain_core/tools/convert.py`

Added imports for `RootModel` (from pydantic) and `is_typeddict` (from typing_extensions).

Before the existing schema-branch logic in `convert_runnable_to_tool`, added a check that detects when `runnable.input_schema` is a `RootModel` wrapping a `TypedDict`. When detected, the `TypedDict` fields are extracted and used to create a flat `BaseModel` via `create_model()`, with the tool name as the model title. This runs only when no explicit `args_schema` was provided by the caller, so existing behavior for custom schemas and `arg_types` is fully preserved.

### `libs/core/tests/unit_tests/test_tools.py`

Added

### From PR #38762
PR #38762 | File: libs/partners/openai/langchain_openai/chat_models/base.py | Line: None
Code Context:
@@ -4056,11 +4102,13 @@ def _create_usage_metadata(
     if service_tier is not None:
         # Avoid counting cache and reasoning tokens towards the service tier token
         # counts, since service tier tokens are already priced differently
-        input_token_details[service_tier] = input_tokens - input_token_details.get(
-            f"{service_tier_prefix}cache_read", 0
+        input_token_details[service_tier] = (
+            input_tokens
+            - (input_token_details.get(f"{service_tier_prefix}cache_read", 0) or 0)
+            - (input_token_details.get(f"{service_tier_prefix}cache_creation", 0) or 0)
         )
Reviewer (open-swe): <!-- open-swe-review-comment {"id":"f_b5c1b2b511","file_path":"libs/partners/openai/langchain_openai/chat_models/base.py","start_line":4106,"end_line":4109,"side":"RIGHT"} -->

🟡 **Cache writes can make tier usage negative**

`cache_write_tokens` cannot be subtracted from `input_tokens` as if it were a disjoint subset. OpenAI defines it as the **unadjusted** number of prompt tokens written to cache, and one request can write several overlapping prefix breakpoints. For example, a 2,304-token request that newly writes prefixes ending at 1,024 and 2,048 tokens can report 3,072 cache-write tokens, making this calculation emit `prior

### From PR #38803
PR #38803: fix(openai): support o-series models in get_num_tokens_from_messages
## Description

`ChatOpenAI.get_num_tokens_from_messages()` raises `NotImplementedError` for OpenAI's o-series reasoning models (`o1`, `o1-mini`, `o1-preview`, `o3`, `o3-mini`, `o3-pro`, `o4-mini`, etc.), even though these models use the exact same message-overhead token accounting as `gpt-4`/`gpt-5` chat models.

### Root cause

In `libs/partners/openai/langchain_openai/chat_models/base.py`, the per-message/per-name token overhead logic is selected by matching the model name prefix:

```python
elif model.startswith(("gpt-3.5-turbo", "gpt-4", "gpt-5")):
    tokens_per_message = 3
    tokens_per_name = 1
```

Because `"o1"`, `"o3"`, `"o4"` aren't in that prefix tuple, calling `get_num_tokens_from_messages()` on any o-series model falls through to the `else` branch, which raises:

```
NotImplementedError(
    f"get_num_tokens_from_messages() is not presently implemented "
    f"for model {model}."
)
```

This breaks token counting for any user on an o-series model (a common and growing case, since o-series are OpenAI's current reasoning-model line).

### Fix

Added `"o1"`, `"o3"`, `"o4"` to the prefix tuple so o-series models use the same `tokens_per_message = 3`, `tokens_per_name = 1` accounting as gpt-4/gpt-5:

```python
elif model.startswith(("gpt-3.5-turbo", "gpt-4", 

### From PR #38803
PR #38803 Commit: fix(openai): support o-series models in get_num_tokens_from_messages
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38802
PR #38802 Commit: fix(core): prevent mutation of caller message metadata in merge_message_runs and convert_to_openai_messages
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]