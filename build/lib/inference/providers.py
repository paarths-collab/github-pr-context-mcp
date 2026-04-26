# Unified LLM provider adapter.
# Supports: cerebras | openai | anthropic | ollama | groq | gemini
# Configured entirely via environment variables — no code changes needed to switch.

import os
import re
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "cerebras").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.1-8b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # Required only for Ollama

# OpenAI-compatible providers — share the same client interface
_OPENAI_COMPATIBLE = {"cerebras", "openai", "ollama", "groq"}
_RATE_LIMIT_PATTERN = re.compile(
    r"rate limit|too many requests|quota exceeded|429",
    re.IGNORECASE,
)


def _effective_settings(settings: dict | None = None) -> dict[str, str]:
    settings = settings or {}
    provider = str(settings.get("llm_provider") or LLM_PROVIDER).strip().lower()
    model = str(settings.get("llm_model") or LLM_MODEL).strip()
    api_key = str(settings.get("llm_api_key") or "").strip()
    base_url = str(settings.get("llm_base_url") or LLM_BASE_URL).strip()
    return {
        "llm_provider": provider,
        "llm_model": model,
        "llm_api_key": api_key,
        "llm_base_url": base_url,
    }


def chat(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 1024,
    settings: dict | None = None,
) -> str:
    """
    Unified chat completion across all supported providers.

    Args:
        messages:   List of {"role": "user"|"assistant", "content": str}
        system:     Optional system prompt string
        max_tokens: Max tokens to generate

    Returns:
        The assistant's reply as a string.
    """
    try:
        effective = _effective_settings(settings)
        provider = effective["llm_provider"]

        if provider in _OPENAI_COMPATIBLE:
            return _openai_compatible(messages, system, max_tokens, effective)
        elif provider == "anthropic":
            return _anthropic(messages, system, max_tokens, effective)
        elif provider == "gemini":
            return _gemini(messages, system, max_tokens, effective)
        else:
            raise ValueError(
                f"Unknown LLM_PROVIDER: '{provider}'. "
                "Valid options: cerebras, openai, anthropic, ollama, groq, gemini"
            )
    except Exception as error:
        retry_message = _format_rate_limit_message(error, settings=settings)
        if retry_message:
            raise RuntimeError(retry_message) from error
        raise


# ── OpenAI-compatible (Cerebras, OpenAI, Ollama, Groq) ───────────────────────

def _build_openai_client(settings: dict[str, str]):
    """Return the right OpenAI-compatible client for the configured provider."""
    provider = settings["llm_provider"]
    if provider == "cerebras":
        try:
            from cerebras.cloud.sdk import Cerebras
            return Cerebras(api_key=_require_key("CEREBRAS_API_KEY", "cloud.cerebras.ai", settings))
        except ImportError:
            raise ImportError("Run: pip install cerebras-cloud-sdk")

    elif provider == "groq":
        try:
            from openai import OpenAI
            return OpenAI(
                api_key=_require_key("GROQ_API_KEY", "console.groq.com/keys", settings),
                base_url="https://api.groq.com/openai/v1",
            )
        except ImportError:
            raise ImportError("Run: pip install openai")

    elif provider == "ollama":
        try:
            from openai import OpenAI
            base_url = settings.get("llm_base_url") or "http://localhost:11434/v1"
            return OpenAI(base_url=base_url, api_key="ollama")
        except ImportError:
            raise ImportError("Run: pip install openai")

    else:  # openai
        try:
            from openai import OpenAI
            return OpenAI(api_key=_require_key("OPENAI_API_KEY", "platform.openai.com", settings))
        except ImportError:
            raise ImportError("Run: pip install openai")


def _openai_compatible(messages: list[dict], system: str, max_tokens: int, settings: dict[str, str]) -> str:
    client = _build_openai_client(settings)
    full_messages = (
        [{"role": "system", "content": system}] if system else []
    ) + messages

    response = client.chat.completions.create(
        model=settings["llm_model"],
        max_tokens=max_tokens,
        messages=full_messages,
    )
    return response.choices[0].message.content


# ── Anthropic ──────────────────────────────────────────────────────────────────

def _anthropic(messages: list[dict], system: str, max_tokens: int, settings: dict[str, str]) -> str:
    try:
        import anthropic
    except ImportError:
        raise ImportError("Run: pip install anthropic")

    client = anthropic.Anthropic(
        api_key=_require_key("ANTHROPIC_API_KEY", "console.anthropic.com", settings)
    )
    kwargs = {"model": settings["llm_model"], "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return response.content[0].text


# ── Gemini ────────────────────────────────────────────────────────────────────

def _gemini(messages: list[dict], system: str, max_tokens: int, settings: dict[str, str]) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError("Run: pip install google-generativeai")

    genai.configure(api_key=_require_key("GEMINI_API_KEY", "aistudio.google.com", settings))

    model = genai.GenerativeModel(
        model_name=settings["llm_model"],
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

def _require_key(env_var: str, signup_url: str, settings: dict[str, str] | None = None) -> str:
    """
    Get an API key from env. Checks the provider-specific var first,
    then falls back to the universal LLM_API_KEY so users only need
    to change one value when switching providers.
    """
    settings = settings or {}
    value = settings.get("llm_api_key") or os.getenv(env_var) or os.getenv("LLM_API_KEY")
    if not value:
        raise EnvironmentError(
            f"No API key found. Set either '{env_var}' or 'LLM_API_KEY' in your .env file.\n"
            f"Get your key at: {signup_url}"
        )
    return value


def _format_rate_limit_message(error: Exception, settings: dict | None = None) -> str | None:
    """Turn provider rate-limit errors into a reset-time hint."""
    if not _looks_like_rate_limit(error):
        return None

    retry_after_seconds, reset_at_text = _rate_limit_reset_hint(error)
    provider_name = _effective_settings(settings)["llm_provider"].capitalize()

    if retry_after_seconds is not None:
        retry_hours = max(retry_after_seconds / 3600, 0.0)
        if retry_hours < 1:
            retry_minutes = max(round(retry_after_seconds / 60), 1)
            return (
                f"{provider_name} rate limit reached. Try again after about "
                f"{retry_minutes} minutes."
            )
        return (
            f"{provider_name} rate limit reached. Try again after about "
            f"{retry_hours:.1f} hours."
        )

    if reset_at_text:
        return (
            f"{provider_name} rate limit reached. Limit resets at {reset_at_text}. "
            "Try again after that reset time."
        )

    return (
        f"{provider_name} rate limit reached. Check the provider dashboard for "
        "when the quota resets and try again after that."
    )


def _looks_like_rate_limit(error: Exception) -> bool:
    """Detect common provider quota and throttling failures."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True

    response_headers = getattr(response, "headers", None) or {}
    if any(key.lower() in {"retry-after", "x-ratelimit-reset"} for key in response_headers):
        return True

    return bool(_RATE_LIMIT_PATTERN.search(str(error)))


def _rate_limit_reset_hint(error: Exception) -> tuple[int | None, str | None]:
    """Extract retry timing hints from the provider exception when available."""
    header_sources = []

    response = getattr(error, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers:
            header_sources.append(headers)

    headers = getattr(error, "headers", None)
    if headers:
        header_sources.append(headers)

    retry_after = None
    reset_at = None

    for header_map in header_sources:
        for key, value in header_map.items():
            normalized_key = key.lower()
            if normalized_key == "retry-after" and retry_after is None:
                retry_after = value
            elif normalized_key == "x-ratelimit-reset" and reset_at is None:
                reset_at = value

    retry_after_seconds = _coerce_retry_after_seconds(retry_after)
    if retry_after_seconds is not None:
        return retry_after_seconds, None

    reset_seconds = _coerce_retry_after_seconds(reset_at)
    if reset_seconds is not None:
        remaining_seconds = int(round(reset_seconds - time.time()))
        if remaining_seconds > 0:
            reset_time = datetime.fromtimestamp(reset_seconds, tz=timezone.utc)
            reset_text = reset_time.strftime("%Y-%m-%d %H:%M UTC")
            return remaining_seconds, reset_text

    return None, None


def _coerce_retry_after_seconds(value: object) -> int | None:
    """Convert a retry/reset header into seconds when possible."""
    if value is None:
        return None

    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
