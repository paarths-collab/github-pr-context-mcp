Repository: langchain-ai/langchain

Issue to resolve:
fix(langchain): only retry retryable exceptions in `ToolRetryMiddleware`

When `on_failure="continue"` in `ToolRetryMiddleware`, we currently normalize all exceptions to ToolMessages, regardless of whether they are specified in `retry_on`. This is a bug: we should only normalize an exception to a ToolMessage if it's specified in `retry_on`. Otherwise, this setting is not very useful as it catches all exceptions raised in tools, which is rarely the correct thing to do. Here we fix this behavior.

If you want to convert exceptions to ToolMessages after retrying them, add the exception type to `retry_on`. If you want to broadly catch all exceptions, use `ToolErrorMiddleware` (added in https://github.com/langchain-ai/langchain/pull/38781), potentially composed with `ToolRetryMiddleware`:
```python
def on_error(exc: Exception, request: ToolCallRequest) -> str:
    tool_name = request.tool_call["name"]
    exc_type = type(exc).__name__
    exc_msg = str(exc)
    return f"Tool '{tool_name}' failed after 1 attempt with {exc_type}: {exc_msg}. Please try again."

agent = create_agent(
    model,
    tools=[...],
    middleware=[
        ToolErrorMiddleware(on_error=on_error),
        ToolRetryMiddleware(retry_on=(MyError,), max_retries=3, on_failure="error"),
    ],
)
```

The change must be confined to these files:
  - libs/langchain_v1/langchain/agents/middleware/tool_retry.py
  - libs/langchain_v1/tests/unit_tests/agents/middleware/implementations/test_tool_retry.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.