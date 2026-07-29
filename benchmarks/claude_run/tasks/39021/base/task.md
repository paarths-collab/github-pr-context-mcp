Repository: langchain-ai/langchain

Issue to resolve:
fix(anthropic): enable structured output for Claude Opus 4.8

[Anthropic's structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) lists `claude-opus-4-8` as supporting native structured output, but `ChatAnthropic` currently reports that capability as disabled.

This updates the Anthropic profile augmentation and regenerates the model profile so `ChatAnthropic(model="claude-opus-4-8").profile["structured_output"]` is `True`.

The change must be confined to these files:
  - libs/partners/anthropic/langchain_anthropic/data/_profiles.py
  - libs/partners/anthropic/langchain_anthropic/data/profile_augmentations.toml
  - libs/partners/anthropic/tests/unit_tests/test_chat_models.py

For each file, give the new or modified code (full function or class bodies, not a diff). Include any test you would add.