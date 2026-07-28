"""Local GitHub App Device Flow with OS-vault credential storage.

This module deliberately supports the public-client portion of GitHub App Device
Flow only.  A downloadable stdio MCP must never embed a GitHub App private key
or client secret.  GitHub App user tokens may expire; local mode fails closed
and asks the user to authorize again instead of silently weakening that model.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import httpx

from .product_github_app import get_product_github_app


DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
KEYRING_SERVICE = "github-pr-context-mcp.github-app"
DEFAULT_PROFILE = "default"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
APP_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")
EXPIRY_SKEW = timedelta(seconds=60)


class GitHubAuthorizationError(RuntimeError):
    """A safe, user-actionable GitHub authorization failure."""


class GitHubAuthorizationRequired(GitHubAuthorizationError):
    """Raised when indexing needs a GitHub connection that is not available."""


class CredentialStoreUnavailable(GitHubAuthorizationError):
    """Raised when the local operating-system credential vault is unavailable."""


class SecretStore(Protocol):
    """Minimal interface used to keep tests independent of a real keyring."""

    def read(self, account: str) -> str | None:
        """Return the secret value for an account, or None when it is absent."""

    def write(self, account: str, secret: str) -> None:
        """Store a secret value for an account."""

    def delete(self, account: str) -> None:
        """Remove a secret value for an account if it exists."""


class KeyringSecretStore:
    """Use the platform keyring without ever falling back to a plaintext file."""

    def __init__(self, service_name: str = KEYRING_SERVICE):
        self._service_name = service_name

    @staticmethod
    def _keyring() -> Any:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise CredentialStoreUnavailable(
                "Secure credential storage is unavailable. Reinstall "
                "github-pr-context-mcp with its keyring dependency."
            ) from exc
        return keyring

    def read(self, account: str) -> str | None:
        try:
            return self._keyring().get_password(self._service_name, account)
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "The operating-system credential vault is unavailable. "
                "Unlock or configure it, then try GitHub authorization again."
            ) from exc

    def write(self, account: str, secret: str) -> None:
        try:
            self._keyring().set_password(self._service_name, account, secret)
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "The operating-system credential vault rejected the GitHub "
                "credential. No credential was saved."
            ) from exc

    def delete(self, account: str) -> None:
        try:
            keyring = self._keyring()
            try:
                keyring.delete_password(self._service_name, account)
            except Exception as exc:
                # Keyring backends use backend-specific exception types for a
                # missing entry. Confirm absence before treating it as an error.
                if keyring.get_password(self._service_name, account) is not None:
                    raise exc
        except CredentialStoreUnavailable:
            raise
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "The operating-system credential vault could not remove the "
                "GitHub credential."
            ) from exc


@dataclass(frozen=True)
class GitHubAppConfig:
    """Non-secret configuration for the shared GitHub App Device Flow client."""

    client_id: str | None
    profile: str = DEFAULT_PROFILE
    app_slug: str | None = None
    app_name: str = "GitHub PR Context"
    source: str = "unconfigured"

    @classmethod
    def from_environment(cls) -> "GitHubAppConfig":
        prohibited = (
            "GITHUB_APP_CLIENT_SECRET",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_PRIVATE_KEY_PATH",
        )
        configured_secret = next(
            (name for name in prohibited if os.getenv(name, "").strip()), None
        )
        if configured_secret:
            raise GitHubAuthorizationError(
                f"{configured_secret} is not supported in local MCP mode. "
                "Do not put a GitHub App secret or private key in a downloaded "
                "MCP configuration."
            )

        product_app = get_product_github_app()
        client_id_override = os.getenv("GITHUB_APP_CLIENT_ID", "").strip() or None
        app_slug_override = os.getenv("GITHUB_APP_SLUG", "").strip() or None
        client_id = client_id_override or product_app.client_id
        app_slug = app_slug_override or product_app.slug
        source = (
            "developer_override"
            if client_id_override
            else "bundled_product_app"
            if client_id
            else "unconfigured"
        )
        if client_id and (len(client_id) > 128 or any(char.isspace() for char in client_id)):
            raise GitHubAuthorizationError(
                "GITHUB_APP_CLIENT_ID is invalid. Copy the GitHub App Client ID "
                "exactly, without spaces."
            )
        if client_id and "your_github_app_client_id" in client_id.lower():
            raise GitHubAuthorizationError(
                "Replace the GITHUB_APP_CLIENT_ID example value with the Client ID "
                "from your GitHub App settings."
            )
        if app_slug and not APP_SLUG_RE.fullmatch(app_slug):
            raise GitHubAuthorizationError(
                "GITHUB_APP_SLUG is invalid. Use the GitHub App URL slug without spaces."
            )

        profile = os.getenv("GITHUB_CREDENTIAL_PROFILE", DEFAULT_PROFILE).strip()
        if not profile:
            profile = DEFAULT_PROFILE
        if not PROFILE_RE.fullmatch(profile):
            raise GitHubAuthorizationError(
                "GITHUB_CREDENTIAL_PROFILE may contain only letters, numbers, "
                "dots, underscores, and hyphens (maximum 64 characters)."
            )
        return cls(
            client_id=client_id,
            profile=profile,
            app_slug=app_slug,
            app_name=product_app.name,
            source=source,
        )

    @property
    def enabled(self) -> bool:
        return self.client_id is not None

    @property
    def installation_url(self) -> str | None:
        if not self.app_slug:
            return None
        return f"https://github.com/apps/{self.app_slug}/installations/new"


@dataclass(frozen=True)
class PendingDeviceAuthorization:
    """In-memory challenge state; the device code is never returned to an MCP client."""

    device_code: str
    verification_uri: str
    user_code: str
    expires_at: datetime
    poll_interval_seconds: int
    next_poll_at: datetime

    def safe_result(self) -> dict[str, Any]:
        return {
            "status": "authorization_pending",
            "verification_uri": self.verification_uri,
            "user_code": self.user_code,
            "expires_at": self.expires_at.isoformat(),
            "poll_interval_seconds": self.poll_interval_seconds,
            "message": (
                "Open verification_uri, enter user_code, approve the GitHub App, "
                "then call complete_github_authorization."
            ),
        }


@dataclass(frozen=True)
class GitHubCredential:
    """A credential that is serialized only inside the operating-system vault."""

    access_token: str
    refresh_token: str | None
    login: str
    github_user_id: str
    expires_at: datetime | None
    refresh_expires_at: datetime | None = None

    def to_secret(self) -> str:
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "login": self.login,
            "github_user_id": self.github_user_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_expires_at": (
                self.refresh_expires_at.isoformat() if self.refresh_expires_at else None
            ),
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_secret(cls, value: str) -> "GitHubCredential":
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict):
                raise ValueError("credential is not an object")
            access_token = payload.get("access_token")
            login = payload.get("login")
            github_user_id = payload.get("github_user_id")
            if not all(isinstance(item, str) and item.strip() for item in (access_token, login, github_user_id)):
                raise ValueError("credential fields are missing")

            refresh_token = payload.get("refresh_token")
            if refresh_token is not None and not isinstance(refresh_token, str):
                raise ValueError("refresh token is invalid")

            expires_at_raw = payload.get("expires_at")
            expires_at = None
            if expires_at_raw is not None:
                if not isinstance(expires_at_raw, str):
                    raise ValueError("expiry is invalid")
                expires_at = _parse_timestamp(expires_at_raw)

            refresh_expires_at_raw = payload.get("refresh_expires_at")
            refresh_expires_at = None
            if refresh_expires_at_raw is not None:
                if not isinstance(refresh_expires_at_raw, str):
                    raise ValueError("refresh expiry is invalid")
                refresh_expires_at = _parse_timestamp(refresh_expires_at_raw)

            return cls(
                access_token=access_token,
                refresh_token=refresh_token or None,
                login=login,
                github_user_id=github_user_id,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GitHubAuthorizationRequired(
                "The saved GitHub connection is invalid. Run disconnect_github, "
                "then begin_github_authorization again."
            ) from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expiration_from_seconds(value: Any, now: datetime) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise GitHubAuthorizationError("GitHub returned an invalid credential expiry.")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise GitHubAuthorizationError("GitHub returned an invalid credential expiry.") from exc
    if seconds <= 0 or seconds > 31_622_400:
        raise GitHubAuthorizationError("GitHub returned an invalid credential expiry.")
    return now + timedelta(seconds=seconds)


class GitHubDeviceFlowService:
    """Authorize a local GitHub App connection and resolve a safe API token."""

    def __init__(
        self,
        config: GitHubAppConfig | None = None,
        secret_store: SecretStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._config = config if config is not None else GitHubAppConfig.from_environment()
        self._secret_store = secret_store
        if self._config.enabled and self._secret_store is None:
            self._secret_store = KeyringSecretStore()
        self._clock = clock or _utc_now
        self._pending: PendingDeviceAuthorization | None = None
        self._pending_lock = threading.Lock()
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_lock_loop: Any | None = None
        self._refresh_lock_guard = threading.Lock()

    @property
    def configured(self) -> bool:
        return self._config.enabled

    @property
    def _account(self) -> str:
        if not self._config.client_id:
            raise GitHubAuthorizationRequired(
                self._unconfigured_message()
            )
        return f"client:{self._config.client_id}:profile:{self._config.profile}"

    def _require_secret_store(self) -> SecretStore:
        if self._secret_store is None:
            raise CredentialStoreUnavailable(
                "Secure credential storage is unavailable for this GitHub App connection."
            )
        return self._secret_store

    async def _read_credential(self) -> GitHubCredential | None:
        if not self.configured:
            return None
        raw = await asyncio.to_thread(self._require_secret_store().read, self._account)
        return GitHubCredential.from_secret(raw) if raw else None

    async def _write_credential(self, credential: GitHubCredential) -> None:
        await asyncio.to_thread(
            self._require_secret_store().write,
            self._account,
            credential.to_secret(),
        )

    async def _delete_credential(self) -> None:
        if not self.configured:
            return
        await asyncio.to_thread(self._require_secret_store().delete, self._account)

    def _get_pending(self) -> PendingDeviceAuthorization | None:
        with self._pending_lock:
            return self._pending

    def _set_pending(self, pending: PendingDeviceAuthorization | None) -> None:
        with self._pending_lock:
            self._pending = pending

    def _refresh_lock_for_current_loop(self) -> asyncio.Lock:
        """Serialize refresh-token rotation within one MCP process/event loop."""
        loop = asyncio.get_running_loop()
        with self._refresh_lock_guard:
            if self._refresh_lock is None or self._refresh_lock_loop is not loop:
                self._refresh_lock = asyncio.Lock()
                self._refresh_lock_loop = loop
            return self._refresh_lock

    async def _post_json(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    data=data,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "github-pr-context-mcp",
                    },
                )
        except httpx.HTTPError as exc:
            raise GitHubAuthorizationError(
                "Could not reach GitHub authorization. Check your network and try again."
            ) from exc

        if response.status_code >= 400:
            raise GitHubAuthorizationError(
                "GitHub rejected the authorization request. Verify the GitHub App "
                "Client ID and that Device Flow is enabled."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubAuthorizationError(
                "GitHub returned an unreadable authorization response. Try again."
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubAuthorizationError(
                "GitHub returned an invalid authorization response. Try again."
            )
        return payload

    async def _get_identity(self, access_token: str) -> tuple[str, str]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    GITHUB_USER_URL,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": "github-pr-context-mcp",
                    },
                )
        except httpx.HTTPError as exc:
            raise GitHubAuthorizationError(
                "GitHub could not verify the connected account. Check your network "
                "and try authorization again."
            ) from exc

        if response.status_code != 200:
            raise GitHubAuthorizationError(
                "GitHub could not verify the connected account. Start a new GitHub "
                "authorization and approve the requested repositories."
            )
        try:
            payload = response.json()
            login = payload.get("login")
            user_id = payload.get("id")
        except (ValueError, AttributeError) as exc:
            raise GitHubAuthorizationError(
                "GitHub returned an invalid account identity. Try authorization again."
            ) from exc
        if not isinstance(login, str) or not login.strip() or user_id is None:
            raise GitHubAuthorizationError(
                "GitHub returned an invalid account identity. Try authorization again."
            )
        return login, str(user_id)

    def _app_metadata(self) -> dict[str, Any]:
        """Return public App metadata that helps an IDE guide a new user."""
        result: dict[str, Any] = {
            "app_name": self._config.app_name,
            "app_configuration_source": self._config.source,
        }
        installation_url = self._config.installation_url
        if installation_url:
            result["app_installation_url"] = installation_url
        return result

    def _with_app_metadata(self, result: dict[str, Any]) -> dict[str, Any]:
        return {**result, **self._app_metadata()}

    def _unconfigured_message(self) -> str:
        return (
            "This build has not been configured with the product GitHub App yet. "
            "A release maintainer must set the public Client ID and App slug in "
            "auth/product_github_app.py before publishing. End users should not "
            "create an App or paste a GitHub token."
        )

    def _safe_connected_result(
        self, credential: GitHubCredential, message: str
    ) -> dict[str, Any]:
        return self._with_app_metadata({
            "status": "connected",
            "connected": True,
            "mode": "github_app_device_flow",
            "login": credential.login,
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
            "message": message,
        })

    @staticmethod
    def _access_is_current(credential: GitHubCredential, now: datetime) -> bool:
        return credential.expires_at is None or credential.expires_at > now + EXPIRY_SKEW

    async def _refresh_expiring_credential(
        self, credential: GitHubCredential
    ) -> GitHubCredential:
        """Rotate a Device-Flow credential using only the public Client ID.

        GitHub permits Device-Flow refreshes without a client secret. Both the old
        and replacement refresh tokens stay exclusively in the OS credential vault.
        """
        now = self._clock()
        if self._access_is_current(credential, now):
            return credential

        async with self._refresh_lock_for_current_loop():
            latest = await self._read_credential()
            if latest is None:
                raise GitHubAuthorizationRequired(
                    "GitHub is not connected. Call begin_github_authorization and "
                    "approve the App in GitHub."
                )
            now = self._clock()
            if self._access_is_current(latest, now):
                return latest
            if not latest.refresh_token or (
                latest.refresh_expires_at
                and latest.refresh_expires_at <= now + EXPIRY_SKEW
            ):
                raise GitHubAuthorizationRequired(
                    "The GitHub approval expired. Call begin_github_authorization "
                    "to connect GitHub again."
                )

            payload = await self._post_json(
                ACCESS_TOKEN_URL,
                {
                    "client_id": self._config.client_id or "",
                    "grant_type": "refresh_token",
                    "refresh_token": latest.refresh_token,
                },
            )
            if payload.get("error"):
                raise GitHubAuthorizationRequired(
                    "GitHub could not refresh this approval. Call "
                    "begin_github_authorization to connect again."
                )

            access_token = payload.get("access_token")
            refresh_token = payload.get("refresh_token")
            if (
                not isinstance(access_token, str)
                or not access_token.strip()
                or not isinstance(refresh_token, str)
                or not refresh_token.strip()
            ):
                raise GitHubAuthorizationRequired(
                    "GitHub returned an incomplete refreshed approval. Call "
                    "begin_github_authorization to connect again."
                )

            expires_at = _expiration_from_seconds(payload.get("expires_in"), now)
            refresh_expires_at = _expiration_from_seconds(
                payload.get("refresh_token_expires_in"), now
            )
            login, github_user_id = await self._get_identity(access_token)
            refreshed = GitHubCredential(
                access_token=access_token,
                refresh_token=refresh_token,
                login=login,
                github_user_id=github_user_id,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
            )
            await self._write_credential(refreshed)
            return refreshed

    async def connection_status(self) -> dict[str, Any]:
        """Return connection metadata only; never return access, refresh, or device tokens."""
        if not self.configured:
            return self._with_app_metadata({
                "status": "not_configured",
                "connected": False,
                "mode": "github_app_device_flow",
                "message": self._unconfigured_message(),
            })

        pending = self._get_pending()
        now = self._clock()
        if pending:
            if pending.expires_at <= now:
                self._set_pending(None)
            else:
                return self._with_app_metadata({
                    "status": "authorization_pending",
                    "connected": False,
                    "mode": "github_app_device_flow",
                    "expires_at": pending.expires_at.isoformat(),
                    "message": "Complete the GitHub browser approval, then call complete_github_authorization.",
                })

        credential = await self._read_credential()
        if credential is None:
            return self._with_app_metadata({
                "status": "disconnected",
                "connected": False,
                "mode": "github_app_device_flow",
                "message": "Call begin_github_authorization to connect GitHub on this machine.",
            })
        if not self._access_is_current(credential, now):
            try:
                credential = await self._refresh_expiring_credential(credential)
            except GitHubAuthorizationRequired as exc:
                return self._with_app_metadata({
                    "status": "reauthorization_required",
                    "connected": False,
                    "mode": "github_app_device_flow",
                    "login": credential.login,
                    "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
                    "message": str(exc),
                })
        return self._safe_connected_result(credential, "GitHub is connected through the local OS credential vault.")

    async def begin_authorization(self) -> dict[str, Any]:
        """Request a one-time GitHub Device Flow challenge without exposing its device code."""
        if not self.configured:
            raise GitHubAuthorizationRequired(
                self._unconfigured_message()
            )

        existing = await self._read_credential()
        now = self._clock()
        if existing:
            try:
                existing = await self._refresh_expiring_credential(existing)
            except GitHubAuthorizationRequired:
                # The next Device Flow challenge deliberately replaces an expired or
                # revoked local approval. Other errors (such as a network outage)
                # still surface to the user instead of creating a duplicate flow.
                pass
            else:
                return self._safe_connected_result(existing, "GitHub is already connected.")

        payload = await self._post_json(DEVICE_CODE_URL, {"client_id": self._config.client_id or ""})
        try:
            device_code = payload["device_code"]
            user_code = payload["user_code"]
            verification_uri = payload["verification_uri"]
            expires_in = int(payload["expires_in"])
            interval = int(payload.get("interval", 5))
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubAuthorizationError(
                "GitHub returned an invalid Device Flow challenge. Try again."
            ) from exc

        parsed_uri = urlparse(verification_uri if isinstance(verification_uri, str) else "")
        if (
            not isinstance(device_code, str)
            or not device_code
            or not isinstance(user_code, str)
            or not user_code
            or parsed_uri.scheme != "https"
            or parsed_uri.netloc != "github.com"
            or expires_in <= 0
            or expires_in > 3600
            or interval < 1
            or interval > 60
        ):
            raise GitHubAuthorizationError(
                "GitHub returned an invalid Device Flow challenge. Try again."
            )

        pending = PendingDeviceAuthorization(
            device_code=device_code,
            verification_uri=verification_uri,
            user_code=user_code,
            expires_at=now + timedelta(seconds=expires_in),
            poll_interval_seconds=interval,
            next_poll_at=now + timedelta(seconds=interval),
        )
        self._set_pending(pending)
        result = pending.safe_result()
        installation_url = self._config.installation_url
        if installation_url:
            result["message"] = (
                "If you have not installed the GitHub App yet, first open "
                "app_installation_url and select only the repositories you want to "
                "share. Then open verification_uri, enter user_code, approve the "
                "GitHub App, and call complete_github_authorization."
            )
        return self._with_app_metadata(result)

    async def complete_authorization(self) -> dict[str, Any]:
        """Poll once for a pending Device Flow challenge and save a successful credential."""
        if not self.configured:
            raise GitHubAuthorizationRequired(
                self._unconfigured_message()
            )
        pending = self._get_pending()
        if pending is None:
            return {
                "status": "disconnected",
                "connected": False,
                "mode": "github_app_device_flow",
                "message": "No GitHub authorization is pending. Call begin_github_authorization first.",
            }

        now = self._clock()
        if pending.expires_at <= now:
            self._set_pending(None)
            return {
                "status": "authorization_expired",
                "connected": False,
                "mode": "github_app_device_flow",
                "message": "The GitHub verification code expired. Call begin_github_authorization again.",
            }

        if now < pending.next_poll_at:
            retry_after = max(
                1,
                math.ceil((pending.next_poll_at - now).total_seconds()),
            )
            return {
                "status": "authorization_pending",
                "connected": False,
                "mode": "github_app_device_flow",
                "expires_at": pending.expires_at.isoformat(),
                "poll_interval_seconds": pending.poll_interval_seconds,
                "retry_after_seconds": retry_after,
                "message": "Wait for retry_after_seconds before polling GitHub again.",
            }

        payload = await self._post_json(
            ACCESS_TOKEN_URL,
            {
                "client_id": self._config.client_id or "",
                "device_code": pending.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        error = payload.get("error")
        if error:
            if error == "authorization_pending":
                self._set_pending(
                    PendingDeviceAuthorization(
                        device_code=pending.device_code,
                        verification_uri=pending.verification_uri,
                        user_code=pending.user_code,
                        expires_at=pending.expires_at,
                        poll_interval_seconds=pending.poll_interval_seconds,
                        next_poll_at=now + timedelta(seconds=pending.poll_interval_seconds),
                    )
                )
                return {
                    "status": "authorization_pending",
                    "connected": False,
                    "mode": "github_app_device_flow",
                    "expires_at": pending.expires_at.isoformat(),
                    "poll_interval_seconds": pending.poll_interval_seconds,
                    "message": "GitHub approval is still pending. Finish it in the browser, then try again.",
                }
            if error == "slow_down":
                updated = PendingDeviceAuthorization(
                    device_code=pending.device_code,
                    verification_uri=pending.verification_uri,
                    user_code=pending.user_code,
                    expires_at=pending.expires_at,
                    poll_interval_seconds=pending.poll_interval_seconds + 5,
                    next_poll_at=now + timedelta(seconds=pending.poll_interval_seconds + 5),
                )
                self._set_pending(updated)
                return {
                    "status": "authorization_pending",
                    "connected": False,
                    "mode": "github_app_device_flow",
                    "expires_at": updated.expires_at.isoformat(),
                    "poll_interval_seconds": updated.poll_interval_seconds,
                    "message": "GitHub asked this client to slow down. Wait before calling complete_github_authorization again.",
                }
            if error in {"expired_token", "incorrect_device_code"}:
                self._set_pending(None)
                return {
                    "status": "authorization_expired",
                    "connected": False,
                    "mode": "github_app_device_flow",
                    "message": "The GitHub verification code is no longer valid. Start a new authorization.",
                }
            if error == "access_denied":
                self._set_pending(None)
                return {
                    "status": "authorization_denied",
                    "connected": False,
                    "mode": "github_app_device_flow",
                    "message": "GitHub authorization was denied. Start again if you want to connect.",
                }
            return {
                "status": "authorization_error",
                "connected": False,
                "mode": "github_app_device_flow",
                "message": "GitHub could not complete authorization. Verify Device Flow is enabled and start again.",
            }

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise GitHubAuthorizationError(
                "GitHub returned an invalid authorization result. Start a new authorization."
            )
        refresh_token = payload.get("refresh_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise GitHubAuthorizationError(
                "GitHub returned an invalid authorization result. Start a new authorization."
            )
        expires_at = _expiration_from_seconds(payload.get("expires_in"), now)
        refresh_expires_at = _expiration_from_seconds(
            payload.get("refresh_token_expires_in"), now
        )
        login, github_user_id = await self._get_identity(access_token)
        credential = GitHubCredential(
            access_token=access_token,
            refresh_token=refresh_token or None,
            login=login,
            github_user_id=github_user_id,
            expires_at=expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        await self._write_credential(credential)
        self._set_pending(None)
        return self._safe_connected_result(
            credential,
            "GitHub is connected. Future indexing uses this OS-vault credential.",
        )

    async def disconnect(self) -> dict[str, Any]:
        """Delete the local stored credential; GitHub-side revocation remains user-controlled."""
        self._set_pending(None)
        if not self.configured:
            return self._with_app_metadata({
                "status": "disconnected",
                "connected": False,
                "mode": "github_app_device_flow",
                "message": self._unconfigured_message(),
            })
        await self._delete_credential()
        return self._with_app_metadata({
            "status": "disconnected",
            "connected": False,
            "mode": "github_app_device_flow",
            "message": (
                "The local GitHub credential was removed from the operating-system vault. "
                "You can also revoke the app in GitHub settings if desired."
            ),
        })

    async def get_access_token(self) -> str:
        """Resolve an index-safe token without exposing it through public tool output."""
        if not self.configured:
            raise GitHubAuthorizationRequired(
                self._unconfigured_message()
            )

        credential = await self._read_credential()
        if credential is None:
            raise GitHubAuthorizationRequired(
                "GitHub is not connected. Call begin_github_authorization, approve it "
                "in GitHub, then call complete_github_authorization."
            )
        credential = await self._refresh_expiring_credential(credential)
        return credential.access_token


def build_local_github_auth_service() -> GitHubDeviceFlowService:
    """Create the singleton used by the local MCP process from environment settings."""
    return GitHubDeviceFlowService()
