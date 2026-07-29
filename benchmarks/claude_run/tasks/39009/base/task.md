Repository: langchain-ai/langchain

Issue to resolve:
fix(openai): correct `gpt-5.3-chat-latest` profile

The `gpt-5.3-chat-latest` OpenAI model profile no longer advertises unsupported configurable reasoning effort levels.

---

`gpt-5.3-chat-latest` is OpenAI's non-reasoning GPT-5.3 Instant model, but its generated LangChain profile advertises configurable `reasoning_effort_levels` while also reporting `reasoning_output=False`. Profile-driven clients can therefore offer settings that the model does not support.

OpenAI's [model page](https://developers.openai.com/api/docs/models/gpt-5.3-chat-latest) identifies this alias as GPT-5.3 Instant. Its [launch announcement](https://openai.com/index/gpt-5-3-instant/) separates Instant from Thinking and Pro, while the [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning) says effort support is model-dependent and must be checked on the relevant model page.

The change must be confined to these files:
  - libs/partners/openai/langchain_openai/data/_profiles.py
  - libs/partners/openai/langchain_openai/data/profile_augmentations.toml
  - libs/partners/openai/tests/unit_tests/chat_models/test_base.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.