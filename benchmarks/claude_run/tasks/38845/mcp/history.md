### From PR #38756
PR #38756: Add first-class retry configuration to ChatOllama 
#38045

This PR adds support for a constructor-level max_retries parameter to ChatOllama, bringing its behavior in line with other LangChain chat integrations.

Changes
Added max_retries as a first-class model field in langchain_ollama/chat_models.py.
Applied the retry policy to both synchronous and asynchronous Ollama client calls.
Added unit tests covering the new behavior in tests/unit_tests/test_chat_models.py.

This enables provider-agnostic retry configuration while preserving the existing API and aligns ChatOllama with the retry semantics used by other LangChain chat models.

### Verification

- ✅ Ran `make format` and `make lint` in `libs/partners/ollama`
- ✅ Added unit tests covering constructor-level `max_retries` behavior
- ✅ Verified `max_retries` is retained as a `ChatOllama` model field
- ✅ Confirmed retry policy is applied to both synchronous and asynchronous client calls
- ✅ Backward compatible — existing code without `max_retries` continues to work


[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38824
PR #38824 Commit: fix(langchain): validate structured output against raw JSON schema dicts

When `response_format` carried a raw JSON schema dict, `_parse_with_schema`
returned the model's tool args unvalidated, so `OutputToolBinding.parse` and
`ProviderStrategyBinding.parse` could never raise for dict schemas and the
`StructuredOutputValidationError` + `handle_errors` retry machinery was
structurally unreachable for exactly that schema kind. Schema-violating
output (e.g. a string where the schema requires an array) was returned
silently as `structured_response`.

Dict schemas are now validated with `jsonschema` (`validator_for` honors an
explicit `$schema` key), raising the same `ValueError` shape as the typed
branches so the existing factory error wrapping and retry flow fire
identically for every schema kind. `jsonschema>=4.18.0` becomes a required
runtime dependency; `types-jsonschema` is added to the typing group.

Fixes #38719

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38844
PR #38844: Fix multiple critical bugs across Anthropic, Perplexity, and Agent Middlewares
Fixes three issues: 1. ChatAnthropic.bind_tools mutating caller-provided tool_choice dictionaries. 2. ChatPerplexity Responses route mutating caller's extra_body dict in place. 3. ModelRetryMiddleware and ModelFallbackMiddleware swallowing GraphInterrupt.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38778
PR #38778 Commit: Fix mutation of tool_choice in ChatAnthropic.bind_tools
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

### From PR #38795
PR #38795: fix(anthropic): prevent bind_tools from mutating caller-provided tool_choice dict
## Summary

`ChatAnthropic.bind_tools()` mutates the caller-provided `tool_choice` dictionary when `parallel_tool_calls=False`. The implementation stores a reference to the original dict and later adds `disable_parallel_tool_use` to it, unexpectedly modifying the caller's input.

## Reproduction

```python
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model="claude-sonnet-4-5-20250929", anthropic_api_key="dummy")

tool_choice = {"type": "tool", "name": "GetWeather"}
print("Before:", tool_choice)

model.bind_tools([], tool_choice=tool_choice, parallel_tool_calls=False)
print("After:", tool_choice)
```

**Before fix:** `{'type': 'tool', 'name': 'GetWeather', 'disable_parallel_tool_use': True}`
**After fix:** `{'type': 'tool', 'name': 'GetWeather'}`

## Fix

Create a shallow copy of the `tool_choice` dict before storing it in `kwargs`:

```python
# Before (mutates caller's dict):
kwargs["tool_choice"] = tool_choice

# After (preserves caller's dict):
kwargs["tool_choice"] = tool_choice.copy()
```

## Test Plan

- [ ] Caller-provided `tool_choice` dict remains unchanged after `bind_tools()`
- [ ] Existing `test_anthropic_bind_tools_tool_choice` test continues to pass
- [ ] `disable_parallel_tool_use` is still correctly added to the internal copy

Closes #38779

[Extra

### From PR #38819
PR #38819 Commit: fix(anthropic): prevent bind_tools from mutating tool_choice dictionary
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38816
PR #38816: docs(langchain-core): fix BaseChatMessageHistory.add_message Raises docstring
Fixes #38603

Update the `Raises` section of `BaseChatMessageHistory.add_message` to match the current implementation by documenting that `NotImplementedError` is raised when neither `add_message` nor `add_messages` is implemented.
[Extraction note: GitHub paginated only part of this PR's pullRequests connection(s).]

### From PR #38811
PR #38811: fix(core): preserve required fields resolved via validation_alias in …
…_parse_input (fixes #38780)

Fixes #

---

<!-- Keep the `Fixes #xx` keyword at the very top and update the issue number — this auto-closes the issue on merge. Replace this comment with a 1-2 sentence description of your change. No `# Summary` header; the description is the summary. -->

Read the full contributing guidelines: https://docs.langchain.com/oss/python/contributing/overview

> **All contributions must be in English.** See the [language policy](https://docs.langchain.com/oss/python/contributing/overview#language-policy).

If you paste a large clearly AI generated description here your PR may be IGNORED or CLOSED!

Thank you for contributing to LangChain! Follow these steps to have your pull request considered as ready for review.

1. PR title: Should follow the format: TYPE(SCOPE): DESCRIPTION

  - Examples:
    - fix(anthropic): resolve flag parsing error
    - feat(core): add multi-tenant support
    - test(openai): update API usage tests
  - Allowed TYPE and SCOPE values: https://github.com/langchain-ai/langchain/blob/master/.github/workflows/pr_lint.yml#L15-L33

2. PR description:

  - Write 1-2 sentences that make the change easy to understand: who benefits, what problem they had, and how this solves it. Prefer a simple user story over a long summary.
  -

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