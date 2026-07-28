# Personal access tokens are not used by local v0.3

The public local v0.3 release uses one product-owned GitHub App and GitHub Device Flow. Users do not create a personal access token, paste one into an IDE configuration, or store one in `.env`.

`GITHUB_TOKEN` is intentionally ignored by the local v0.3 credential resolver. This prevents an accidental environment token from silently becoming a user's repository credential.

If you find an older guide or configuration that asks for a GitHub PAT, remove it and follow the [free local GitHub App flow](guides/github-app-device-flow.md) instead.

For a future hosted service, the project must use a tenant-aware GitHub App installation flow and server-side secret manager. A global PAT is not a public-user authentication design.
