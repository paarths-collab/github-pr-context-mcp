from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier

GMAIL_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@gmail\.com$", re.IGNORECASE)
ALLOWED_LLM_PROVIDERS = {"cerebras", "openai", "anthropic", "ollama", "groq", "gemini"}


@dataclass(frozen=True)
class RegistrationResult:
    email: str
    token: str
    settings: dict[str, str]


class GmailIdentityStore:
    """Store one registered bearer token per Gmail address backed by thread-safe SQLite."""

    def __init__(self, file_path: str):
        # Swap existing json suffixes to .db without breaking integrations
        p = Path(file_path)
        if p.suffix == '.json':
            self._path = p.with_suffix('.db')
        else:
            self._path = p
            
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        # isolation_level=None enables autocommit for simple operations
        # check_same_thread=False allows sharing across async workers
        return sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    email TEXT PRIMARY KEY,
                    token_hash TEXT,
                    registered_at TEXT,
                    last_seen TEXT,
                    revoked INTEGER DEFAULT 0,
                    settings TEXT
                )
            ''')

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _normalize_email(self, email: str) -> str:
        candidate = email.strip().lower()
        if not GMAIL_EMAIL_RE.fullmatch(candidate):
            raise ValueError("Only gmail.com addresses are allowed")
        return candidate

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _normalize_optional(self, value: Any, field_name: str, max_len: int = 512) -> str | None:
        if value is None:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        if len(candidate) > max_len:
            raise ValueError(f"{field_name} is too long")
        return candidate

    def _sanitize_settings(self, settings: dict[str, Any] | None) -> dict[str, str]:
        if not settings:
            return {}
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")

        if "github_token" in settings:
            raise ValueError(
                "github_token is not accepted. GitHub credentials must use the "
                "local Device Flow credential vault, not hosted SQLite settings."
            )

        sanitized: dict[str, str] = {}

        llm_provider = self._normalize_optional(settings.get("llm_provider"), "llm_provider", max_len=64)
        if llm_provider:
            provider = llm_provider.lower()
            if provider not in ALLOWED_LLM_PROVIDERS:
                options = ", ".join(sorted(ALLOWED_LLM_PROVIDERS))
                raise ValueError(f"llm_provider must be one of: {options}")
            sanitized["llm_provider"] = provider

        llm_model = self._normalize_optional(settings.get("llm_model"), "llm_model", max_len=128)
        if llm_model:
            sanitized["llm_model"] = llm_model

        llm_api_key = self._normalize_optional(settings.get("llm_api_key"), "llm_api_key")
        if llm_api_key:
            sanitized["llm_api_key"] = llm_api_key

        llm_base_url = self._normalize_optional(settings.get("llm_base_url"), "llm_base_url")
        if llm_base_url:
            lowered = llm_base_url.lower()
            if not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError("llm_base_url must start with http:// or https://")
            sanitized["llm_base_url"] = llm_base_url

        return sanitized

    @staticmethod
    def _drop_legacy_github_token(settings: Any) -> tuple[dict[str, Any], bool]:
        """Remove a pre-v0.3 PAT without ever returning it to a caller."""
        if not isinstance(settings, dict):
            return {}, False
        cleaned = deepcopy(settings)
        removed = "github_token" in cleaned
        cleaned.pop("github_token", None)
        return cleaned, removed

    def _masked_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        masked, _ = self._drop_legacy_github_token(settings)
        for key in ("llm_api_key",):
            if key in masked:
                masked[key] = "***"
        return masked

    def register_email(self, email: str, settings: dict[str, Any] | None = None) -> RegistrationResult:
        normalized_email = self._normalize_email(email)
        sanitized_settings = self._sanitize_settings(settings)
        
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)
        now = self._utc_now()
        settings_json = json.dumps(sanitized_settings)
        
        with self._get_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (email, token_hash, registered_at, last_seen, revoked, settings) VALUES (?, ?, ?, ?, ?, ?)",
                    (normalized_email, token_hash, now, None, 0, settings_json)
                )
            except sqlite3.IntegrityError:
                raise ValueError("This Gmail address is already registered")

        return RegistrationResult(
            email=normalized_email,
            token=token,
            settings=self._masked_settings(sanitized_settings),
        )

    def get_user_settings(self, email: str) -> dict[str, str]:
        normalized_email = self._normalize_email(email)
        with self._get_conn() as conn:
            row = conn.execute("SELECT revoked, settings FROM users WHERE email = ?", (normalized_email,)).fetchone()
            
        if not row:
            return {}
            
        revoked, settings_json = row
        if revoked:
            return {}
            
        try:
            parsed = json.loads(settings_json) if settings_json else {}
        except Exception:
            return {}

        settings, removed_legacy_token = self._drop_legacy_github_token(parsed)
        if removed_legacy_token:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE users SET settings = ? WHERE email = ?",
                    (json.dumps(settings), normalized_email),
                )
        return settings

    def update_user_settings(self, email: str, settings: dict[str, Any]) -> dict[str, str]:
        normalized_email = self._normalize_email(email)
        sanitized_settings = self._sanitize_settings(settings)
        
        with self._get_conn() as conn:
            row = conn.execute("SELECT revoked, settings FROM users WHERE email = ?", (normalized_email,)).fetchone()
            if not row:
                raise ValueError("User not found")
                
            revoked, existing_settings_json = row
            if revoked:
                raise ValueError("User not found")

            existing: dict[str, Any] = {}
            if existing_settings_json:
                try:
                    existing = json.loads(existing_settings_json)
                except Exception:
                    pass
            existing, removed_legacy_token = self._drop_legacy_github_token(existing)

            if sanitized_settings:
                existing.update(sanitized_settings)
            if sanitized_settings or removed_legacy_token:
                conn.execute("UPDATE users SET settings = ? WHERE email = ?", (json.dumps(existing), normalized_email))

        return self._masked_settings(existing)

    def revoke_email(self, email: str) -> bool:
        normalized_email = self._normalize_email(email)
        with self._get_conn() as conn:
            cursor = conn.execute("UPDATE users SET revoked = 1 WHERE email = ?", (normalized_email,))
            return cursor.rowcount > 0

    def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None

        token_hash = self._hash_token(token)
        now = self._utc_now()
        
        with self._get_conn() as conn:
            # We iterate rather than querying hash to handle potentially many rows 
            # Note: For hyper-scale, querying `WHERE token_hash = ?` is better.
            cursor = conn.execute("SELECT email, revoked, token_hash FROM users WHERE revoked = 0")
            matched_email = None
            for email, revoked, stored_hash in cursor:
                if isinstance(stored_hash, str) and hmac.compare_digest(stored_hash, token_hash):
                    matched_email = email
                    break
                    
            if not matched_email:
                return None

            conn.execute("UPDATE users SET last_seen = ? WHERE email = ?", (now, matched_email))

        scopes = [f"identity:{matched_email}"]
        return AccessToken(token=token, client_id=matched_email, scopes=scopes)

    def whoami(self, token: str) -> dict[str, Any] | None:
        token_info = self.verify_token(token)
        if not token_info:
            return None
        return {"email": token_info.client_id, "scopes": token_info.scopes}


class GmailTokenVerifier(TokenVerifier):
    def __init__(self, store: GmailIdentityStore):
        self._store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        return self._store.verify_token(token)
