### From PR #38763
PR #38763: fix(langchain): bound Python file search regex execution
## Summary
- Bound the Python fallback grep search with a killable subprocess timeout
- Preserved regex semantics while preventing catastrophic backtracking from hanging the main process
- Moved the blocking search loop into a stdlib-only worker module
- Added regression coverage for ReDoS timeout behavior and worker failure handling

## Tests
- uv run --directory libs/langchain_v1 pytest tests/unit_tests/agents/middleware/implementations/test_file_search.py -q
- uv run --directory libs/langchain_v1 ruff check langchain/agents/middleware/file_search.py langchain/agents/middleware/_file_search_worker.py tests/unit_tests/agents/middleware/implementations/test_file_search.py
- uv run --directory libs/langchain_v1 ruff format --check langchain/agents/middleware/file_search.py langchain/agents/middleware/_file_search_worker.py tests/unit_tests/agents/middleware/implementations/test_file_search.py

Related to #38737
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

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

### From PR #38773
PR #38773: fix(langchain): export PIIMatch from langchain.agents.middleware public API
## Summary

`PIIMatch` — the return type required for custom callable detectors — was omitted from `langchain.agents.middleware.__init__.py`, forcing users to import from the private `_redaction` module. This PR adds it to the public package and improves its docstring.

Fixes #38718

## Root Cause

`pii.py` correctly lists `PIIMatch` in its own `__all__`. However, the `agents/middleware/__init__.py` re-export line only included `PIIDetectionError` and `PIIMiddleware`:

```python
# before (missing PIIMatch)
from langchain.agents.middleware.pii import PIIDetectionError, PIIMiddleware
```

Users following the documented custom-detector pattern hit an `ImportError` when trying to type their return value with `PIIMatch`.

## Changes

| File | What changed |
|------|-------------|
| `langchain/agents/middleware/__init__.py` | Add `PIIMatch` to import and `__all__` |
| `langchain/agents/middleware/pii.py` | Expand `detector` docstring with `PIIMatch` field names and an inline example |
| `tests/.../test_pii.py` | Add `TestPublicAPIExports` with two tests: public import succeeds, and `PIIMatch` has the correct `type`/`value`/`start`/`end` fields |

## Testing

- [x] Existing tests pass (`TestEmailDetection`, `TestCreditCardDetection`, etc.)
- [x] New test: `test_piimatch_importable_from_public_module

### From PR #32496
PR #32496 | File: libs/core/langchain_core/v1/utils.py | Line: 1
Reviewer (mishra-krishna): This is a fantastic addition! These message display utilities will be incredibly helpful for debugging and understanding the content of messages. The different formatting options and Jupyter integration are excellent features. Great work!
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38763
PR #38763 Commit: fix(langchain): bound Python file search regex execution
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38762
PR #38762 overall review by open-swe [COMMENTED]: **Open SWE Review** found 1 potential issue.

[Open in Web](https://openswe.vercel.app/agents/reviews/langchain-ai/langchain/38762) • [View Open SWE trace](https://smith.langchain.com/o/ebbaf2eb-769b-4505-aca2-d11de10372a4/projects/p/f2e68aea-26b3-4a6a-9c4a-d04e7ebe2690/t/ce2e8a17-16cc-58ff-9a07-2976d9aea63d)

<!-- open-swe-reviewer pr=38762 -->
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38767
PR #38767: feat(middleware): add AskQuestionMiddleware for structured HITL questions
## Summary

- Adds **`AskQuestionMiddleware`** — registers an `AskQuestion` tool that pauses the agent via LangGraph `interrupt()` with a versioned multi-choice payload (`schema_version: 1.1`).
- Adds Pydantic schemas (`QuestionOption`, `Question`, `AskQuestionInput`) with validation for duplicate ids, min/max select, and free-text placeholder rules.
- Adds **`HITLStructuredTool`** helper so `GraphInterrupt` is not reported as a tool error (related: langgraph #8218, #8217).
- Rejects parallel `AskQuestion` calls in one model turn (same guard pattern as `TodoListMiddleware`).
- Unit tests for schemas, interrupt payload, system-prompt injection, and parallel-call guarding.

Closes #38766

## Motivation

`HumanInTheLoopMiddleware` handles **approval of pending tool calls**. Agents also need to **ask structured clarification questions** mid-task. Raw `interrupt("string")` gives UIs no schema to render forms.

This mirrors the `TodoListMiddleware` pattern: middleware registers a tool + injects system guidance.

## Usage

```python
from langchain.agents import create_agent
from langchain.agents.middleware import AskQuestionMiddleware
from langgraph.checkpoint.memory import InMemorySaver

agent = create_agent(
    model,
    middleware=[AskQuestionMiddleware()],
    checkpointer=InMemorySaver(),
)
```

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

### From PR #38767
PR #38767 Commit: feat(middleware): add AskQuestionMiddleware for structured HITL questions

Closes langchain-ai/langchain#38766
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]