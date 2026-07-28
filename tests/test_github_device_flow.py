"""Offline contract tests for local GitHub App Device Flow."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import auth.product_github_app as product_github_app
from auth.github_device_flow import (
    CredentialStoreUnavailable,
    GitHubAppConfig,
    GitHubAuthorizationError,
    GitHubCredential,
    GitHubDeviceFlowService,
)


class MemorySecretStore:
    def __init__(self):
        self.values = {}

    def read(self, account):
        return self.values.get(account)

    def write(self, account, secret):
        self.values[account] = secret

    def delete(self, account):
        self.values.pop(account, None)


class UnavailableSecretStore:
    def read(self, account):
        raise CredentialStoreUnavailable("vault unavailable")

    def write(self, account, secret):
        raise CredentialStoreUnavailable("vault unavailable")

    def delete(self, account):
        raise CredentialStoreUnavailable("vault unavailable")


class FixedClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _service(clock=None, store=None):
    return GitHubDeviceFlowService(
        config=GitHubAppConfig(client_id="Iv1.test-client", profile="test"),
        secret_store=store or MemorySecretStore(),
        clock=clock or FixedClock(datetime(2026, 7, 27, tzinfo=timezone.utc)),
    )


def test_device_flow_returns_only_user_safe_challenge_and_vaults_tokens(monkeypatch):
    store = MemorySecretStore()
    clock = FixedClock(datetime(2026, 7, 27, tzinfo=timezone.utc))
    service = _service(clock=clock, store=store)
    calls = []

    async def fake_post(url, data):
        calls.append((url, data))
        if url.endswith("/device/code"):
            return {
                "device_code": "device-secret-value",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            }
        return {
            "access_token": "access-secret-value",
            "refresh_token": "refresh-secret-value",
            "expires_in": 28800,
        }

    async def fake_identity(access_token):
        assert access_token == "access-secret-value"
        return "octocat", "42"

    monkeypatch.setattr(service, "_post_json", fake_post)
    monkeypatch.setattr(service, "_get_identity", fake_identity)

    challenge = asyncio.run(service.begin_authorization())
    public_challenge = json.dumps(challenge)
    assert challenge["status"] == "authorization_pending"
    assert challenge["verification_uri"] == "https://github.com/login/device"
    assert challenge["user_code"] == "ABCD-EFGH"
    assert "device-secret-value" not in public_challenge

    clock.now += timedelta(seconds=5)
    completed = asyncio.run(service.complete_authorization())
    public_completed = json.dumps(completed)
    assert completed["status"] == "connected"
    assert completed["login"] == "octocat"
    assert "access-secret-value" not in public_completed
    assert "refresh-secret-value" not in public_completed
    assert "device-secret-value" not in public_completed
    assert asyncio.run(service.get_access_token()) == "access-secret-value"
    assert len(store.values) == 1
    assert "access-secret-value" in next(iter(store.values.values()))
    assert calls[1][1]["device_code"] == "device-secret-value"


def test_slow_down_updates_interval_without_exposing_device_code(monkeypatch):
    clock = FixedClock(datetime(2026, 7, 27, tzinfo=timezone.utc))
    service = _service(clock=clock)

    async def fake_post(url, data):
        if url.endswith("/device/code"):
            return {
                "device_code": "device-secret-value",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            }
        return {"error": "slow_down"}

    monkeypatch.setattr(service, "_post_json", fake_post)
    asyncio.run(service.begin_authorization())
    too_early = asyncio.run(service.complete_authorization())
    assert too_early["retry_after_seconds"] == 5

    clock.now += timedelta(seconds=5)
    result = asyncio.run(service.complete_authorization())

    assert result["status"] == "authorization_pending"
    assert result["poll_interval_seconds"] == 10
    assert "device-secret-value" not in json.dumps(result)


def test_expired_credential_without_a_refresh_token_requires_reauthorization():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    clock = FixedClock(now)
    store = MemorySecretStore()
    service = _service(clock=clock, store=store)
    credential = GitHubCredential(
        access_token="expired-access",
        refresh_token=None,
        login="octocat",
        github_user_id="42",
        expires_at=now - timedelta(seconds=1),
    )
    store.write(service._account, credential.to_secret())

    status = asyncio.run(service.connection_status())
    assert status["status"] == "reauthorization_required"
    assert status["connected"] is False
    with pytest.raises(GitHubAuthorizationError, match="approval expired"):
        asyncio.run(service.get_access_token())


def test_expired_device_flow_credential_refreshes_without_a_client_secret(monkeypatch):
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    clock = FixedClock(now)
    store = MemorySecretStore()
    service = _service(clock=clock, store=store)
    expired = GitHubCredential(
        access_token="expired-access",
        refresh_token="old-refresh-secret",
        login="octocat",
        github_user_id="42",
        expires_at=now - timedelta(seconds=1),
        refresh_expires_at=now + timedelta(days=30),
    )
    store.write(service._account, expired.to_secret())
    calls = []

    async def fake_post(url, data):
        calls.append((url, data))
        return {
            "access_token": "refreshed-access-secret",
            "refresh_token": "new-refresh-secret",
            "expires_in": 28800,
            "refresh_token_expires_in": 15897600,
        }

    async def fake_identity(access_token):
        assert access_token == "refreshed-access-secret"
        return "octocat", "42"

    monkeypatch.setattr(service, "_post_json", fake_post)
    monkeypatch.setattr(service, "_get_identity", fake_identity)

    status = asyncio.run(service.connection_status())

    assert status["status"] == "connected"
    assert asyncio.run(service.get_access_token()) == "refreshed-access-secret"
    assert calls == [
        (
            "https://github.com/login/oauth/access_token",
            {
                "client_id": "Iv1.test-client",
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh-secret",
            },
        )
    ]
    assert "client_secret" not in calls[0][1]
    vault_value = next(iter(store.values.values()))
    assert "old-refresh-secret" not in vault_value
    assert "new-refresh-secret" in vault_value


def test_unavailable_vault_has_no_plaintext_fallback():
    service = _service(store=UnavailableSecretStore())

    with pytest.raises(CredentialStoreUnavailable):
        asyncio.run(service.connection_status())
    with pytest.raises(CredentialStoreUnavailable):
        asyncio.run(service.get_access_token())


def test_local_configuration_rejects_github_app_secrets(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_CLIENT_ID", raising=False)
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "do-not-ship-this")

    with pytest.raises(GitHubAuthorizationError, match="not supported in local MCP mode"):
        GitHubAppConfig.from_environment()


def test_local_configuration_rejects_an_unreplaced_client_id_example(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv1_your_github_app_client_id")

    with pytest.raises(GitHubAuthorizationError, match="Replace the GITHUB_APP_CLIENT_ID example"):
        GitHubAppConfig.from_environment()


def test_official_release_can_bundle_a_public_app_without_user_environment(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)
    monkeypatch.delenv("GITHUB_APP_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(product_github_app, "PRODUCT_GITHUB_APP_CLIENT_ID", "Iv1.product-client")
    monkeypatch.setattr(product_github_app, "PRODUCT_GITHUB_APP_SLUG", "github-pr-context")

    config = GitHubAppConfig.from_environment()

    assert config.client_id == "Iv1.product-client"
    assert config.source == "bundled_product_app"
    assert config.installation_url == "https://github.com/apps/github-pr-context/installations/new"


def test_personal_environment_token_is_not_a_local_v3_fallback(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)
    monkeypatch.delenv("GITHUB_APP_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(product_github_app, "PRODUCT_GITHUB_APP_CLIENT_ID", None)
    monkeypatch.setenv("GITHUB_TOKEN", "never-read-by-local-v3")
    service = GitHubDeviceFlowService(
        config=GitHubAppConfig.from_environment(),
        secret_store=MemorySecretStore(),
    )

    status = asyncio.run(service.connection_status())

    assert status["status"] == "not_configured"
    assert status["connected"] is False
    with pytest.raises(GitHubAuthorizationError, match="product GitHub App"):
        asyncio.run(service.get_access_token())
