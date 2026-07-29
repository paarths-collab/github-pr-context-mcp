Repository: langchain-ai/langchain

Issue to resolve:
fix(anthropic): preserve empty thinking field in signature_delta streaming

Fixes #38639

Streamed signature_delta chunks from empty adaptive-thinking blocks were missing the required thinking key, causing 400 errors on replay. Added setdefault.

The change must be confined to these files:
  - libs/partners/anthropic/langchain_anthropic/chat_models.py
  - libs/partners/anthropic/tests/unit_tests/test_chat_models.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.