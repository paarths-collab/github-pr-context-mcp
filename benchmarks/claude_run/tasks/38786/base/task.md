Repository: langchain-ai/langchain

Issue to resolve:
feat(langchain): add `meta` extra and support langchain-meta in init_chat_model

```python
# pip install "langchain[meta]"

model = init_chat_model("meta:muse-spark-1.1")
```

The change must be confined to these files:
  - libs/langchain_v1/langchain/chat_models/base.py
  - libs/langchain_v1/pyproject.toml
  - libs/langchain_v1/uv.lock

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.