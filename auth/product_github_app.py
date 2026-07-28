"""Public GitHub App metadata bundled with official local releases.

Only the GitHub App client ID and slug belong here. They are public identifiers,
not credentials. A release maintainer creates one public GitHub App, replaces the
two ``None`` values below once, and ships that release to every user. Never add a
client secret, private key, access token, or refresh token to this file.

Fork maintainers can either update these values for their own release or use the
``GITHUB_APP_CLIENT_ID`` and ``GITHUB_APP_SLUG`` development overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


# Set these two public values once, immediately before publishing an official
# release. Keeping the source checkout unconfigured prevents it from silently
# claiming an App that has not actually been registered by the maintainer.
PRODUCT_GITHUB_APP_CLIENT_ID: Final[str | None] = None
PRODUCT_GITHUB_APP_SLUG: Final[str | None] = None
PRODUCT_GITHUB_APP_NAME: Final[str] = "GitHub PR Context"


@dataclass(frozen=True)
class ProductGitHubApp:
    """Non-secret metadata for the one GitHub App shared by local users."""

    client_id: str | None
    slug: str | None
    name: str


def get_product_github_app() -> ProductGitHubApp:
    """Return the public App metadata compiled into this local release."""

    return ProductGitHubApp(
        client_id=PRODUCT_GITHUB_APP_CLIENT_ID,
        slug=PRODUCT_GITHUB_APP_SLUG,
        name=PRODUCT_GITHUB_APP_NAME,
    )
