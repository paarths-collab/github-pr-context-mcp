# LLM Configuration

<div align="center">

![Provider Adapter](https://img.shields.io/badge/Adapter-Unified-blue)
![Hot-Swappable](https://img.shields.io/badge/Switch-No%20Code%20Changes-green)

</div>

Set `LLM_PROVIDER` and `LLM_MODEL` in `.env`. The provider layer is unified, so you can switch vendors without touching application code.

If Cerebras returns a quota or rate-limit error, the app now surfaces the reset time when it is available and tells you how long to wait before trying again.

---

## Provider Matrix

| Provider | `LLM_PROVIDER` | Example `LLM_MODEL` | Setup |
|---|---|---|---|
| **Cerebras** | `cerebras` | `llama3.1-8b` | Free tier at [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| **Groq** | `groq` | `llama-3.3-70b-versatile` | Free tier at [console.groq.com](https://console.groq.com/keys) |
| **Gemini** | `gemini` | `gemini-2.5-flash` | Free tier at [aistudio.google.com](https://aistudio.google.com) |
| **OpenAI** | `openai` | `gpt-5` | Paid (best for complex review reasoning) |
| **Anthropic** | `anthropic` | `claude-sonnet-4-6` | Paid (strong long-context quality) |
| **Ollama** | `ollama` | `qwen2.5:7b` | Local and fully self-hosted |

---

## Environment Setup

### Minimal portable setup

```env
LLM_PROVIDER=cerebras
LLM_MODEL=llama3.1-8b
LLM_API_KEY=your_key_here
```

### Provider-specific keys (optional)

| Provider | Optional Env Key |
|---|---|
| Cerebras | `CEREBRAS_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Groq | `GROQ_API_KEY` |
| Gemini | `GEMINI_API_KEY` |

Fallback behavior:
- If a provider-specific key is missing, the server falls back to `LLM_API_KEY`.

## Hosted deployments

In Render or any other shared deployed setup, `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, and `GITHUB_TOKEN` are deployment-owned environment variables.

- End users can change their own MCP auth token.
- End users cannot swap the LLM provider or model from their client config unless you build a separate settings endpoint.
- End users cannot replace the GitHub token in the hosted service; that token belongs to the deployment and is what the server uses to fetch PR data.

### Ollama note

Set `LLM_BASE_URL` only when needed:

```env
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:7b
LLM_BASE_URL=http://localhost:11434/v1
```

---

## Decision Guide

| Priority | Recommended Provider |
|---|---|
| Lowest cost / quick testing | Cerebras, Groq, Gemini |
| Highest review quality | OpenAI, Anthropic |
| Offline / local control | Ollama |

---

Back to [README](../README.md)
