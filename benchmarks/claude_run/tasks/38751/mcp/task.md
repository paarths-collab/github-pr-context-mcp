Repository: langchain-ai/langchain

Issue to resolve:
fix(fireworks): report cached prompt token usage

Closes #38648

`langchain-fireworks` now reports Fireworks cached prompt tokens in `AIMessage.usage_metadata.input_token_details.cache_read` and no longer crashes when combining nested token usage from batched `generate()` calls.

---

Fireworks can return nested token usage details when prompt caching is involved, including cached prompt token counts. Batched `generate()` calls were crashing when those nested dictionaries were combined, and regular chat results did not expose the cached-token breakdown in the standard LangChain usage metadata shape.

This updates `ChatFireworks` so nested token usage is merged safely and cached prompt tokens are reported as `input_token_details.cache_read`. Users and downstream tracing systems can now distinguish cached Fireworks input tokens from regular input tokens instead of treating the full prompt as uncached input.

Thanks to @abcgco for the original report and recursive merge fix in #38646, and to @abhi-0203 for independently identifying the same nested `token_usage` failure in #38735. This PR builds on that work by using the recursive merge approach and extending the fix to normalize cached prompt tokens into standard usage metadata for tracing and cost reporting.

The change must be confined to these files:
  - libs/partners/fireworks/langchain_fireworks/chat_models.py
  - libs/partners/fireworks/tests/unit_tests/test_chat_models.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.