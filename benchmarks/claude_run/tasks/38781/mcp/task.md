Repository: langchain-ai/langchain

Issue to resolve:
feat(langchain): `ToolErrorMiddleware`

Resolves https://github.com/langchain-ai/langchain/issues/37195

Adds a `ToolErrorMiddleware` that allows specification of exceptions to be caught and translated into ToolMessages.

Usage patterns below.

Basic usage: `MyError` becomes `ToolMessage("Tool 'failing_tool' failed with MyError")`
```python
def on_error(exc: Exception, request: ToolCallRequest) -> str | None:
    if isinstance(exc, MyError):
        return f"Tool '{request.tool_call['name']}' failed with {type(exc).__name__}"
    # propagates everything else


agent = create_agent(
    model="...",
    tools=[failing_tool],
    middleware=[ToolErrorMiddleware(on_error)],
)
```

The change must be confined to these files:
  - libs/langchain_v1/langchain/agents/middleware/__init__.py
  - libs/langchain_v1/langchain/agents/middleware/tool_error.py
  - libs/langchain_v1/tests/unit_tests/agents/middleware/implementations/test_tool_error.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.