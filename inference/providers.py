# Unified LLM provider adapter.
# Supports: cerebras | openai | anthropic | ollama | groq | gemini
# Configured entirely via environment variables — no code changes needed to switch.

import os
from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "cerebras").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.1-8b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # Required only for Ollama

# OpenAI-compatible providers — share the same client interface
_OPENAI_COMPATIBLE = {"cerebras", "openai", "ollama", "groq"}


def chat(messages: list[dict], system: str = "", max_tokens: int = 1024) -> str:
    """
    Unified chat completion across all supported providers.

    Args:
        messages:   List of {"role": "user"|"assistant", "content": str}
        system:     Optional system prompt string
        max_tokens: Max tokens to generate

    Returns:
        The assistant's reply as a string.
    """
    if LLM_PROVIDER in _OPENAI_COMPATIBLE:
        return _openai_compatible(messages, system, max_tokens)
    elif LLM_PROVIDER == "anthropic":
        return _anthropic(messages, system, max_tokens)
    elif LLM_PROVIDER == "gemini":
        return _gemini(messages, system, max_tokens)
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER: '{LLM_PROVIDER}'. "
            "Valid options: cerebras, openai, anthropic, ollama, groq, gemini"
        )


# ── OpenAI-compatible (Cerebras, OpenAI, Ollama, Groq) ───────────────────────

def _build_openai_client():
    """Return the right OpenAI-compatible client for the configured provider."""
    if LLM_PROVIDER == "cerebras":
        try:
            from cerebras.cloud.sdk import Cerebras
            return Cerebras(api_key=_require_key("CEREBRAS_API_KEY", "cloud.cerebras.ai"))
        except ImportError:
            raise ImportError("Run: pip install cerebras-cloud-sdk")

    elif LLM_PROVIDER == "groq":
        try:
            from openai import OpenAI
            return OpenAI(
                api_key=_require_key("GROQ_API_KEY", "console.groq.com/keys"),
                base_url="https://api.groq.com/openai/v1",
            )
        except ImportError:
            raise ImportError("Run: pip install openai")

    elif LLM_PROVIDER == "ollama":
        try:
            from openai import OpenAI
            base_url = LLM_BASE_URL or "http://localhost:11434/v1"
            return OpenAI(base_url=base_url, api_key="ollama")
        except ImportError:
            raise ImportError("Run: pip install openai")

    else:  # openai
        try:
            from openai import OpenAI
            return OpenAI(api_key=_require_key("OPENAI_API_KEY", "platform.openai.com"))
        except ImportError:
            raise ImportError("Run: pip install openai")


def _openai_compatible(messages: list[dict], system: str, max_tokens: int) -> str:
    client = _build_openai_client()
    full_messages = (
        [{"role": "system", "content": system}] if system else []
    ) + messages

    response = client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        messages=full_messages,
    )
    return response.choices[0].message.content


# ── Anthropic ──────────────────────────────────────────────────────────────────

def _anthropic(messages: list[dict], system: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError:
        raise ImportError("Run: pip install anthropic")

    client = anthropic.Anthropic(
        api_key=_require_key("ANTHROPIC_API_KEY", "console.anthropic.com")
    )
    kwargs = {"model": LLM_MODEL, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return response.content[0].text


# ── Gemini ────────────────────────────────────────────────────────────────────

def _gemini(messages: list[dict], system: str, max_tokens: int) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError("Run: pip install google-generativeai")

    genai.configure(api_key=_require_key("GEMINI_API_KEY", "aistudio.google.com"))

    model = genai.GenerativeModel(
        model_name=LLM_MODEL,
        system_instruction=system or None,
    )

    # Convert OpenAI-style message list to Gemini's format
    gemini_history = []
    last_user_message = ""

    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        if msg["role"] == "user":
            last_user_message = msg["content"]
        else:
            gemini_history.append({"role": role, "parts": [msg["content"]]})

    chat_session = model.start_chat(history=gemini_history)
    response = chat_session.send_message(
        last_user_message,
        generation_config=genai.GenerationConfig(max_output_tokens=max_tokens),
    )
    return response.text


# ── Helper ─────────────────────────────────────────────────────────────────────

def _require_key(env_var: str, signup_url: str) -> str:
    """
    Get an API key from env. Checks the provider-specific var first,
    then falls back to the universal LLM_API_KEY so users only need
    to change one value when switching providers.
    """
    value = os.getenv(env_var) or os.getenv("LLM_API_KEY")
    if not value:
        raise EnvironmentError(
            f"No API key found. Set either '{env_var}' or 'LLM_API_KEY' in your .env file.\n"
            f"Get your key at: {signup_url}"
        )
    return value
