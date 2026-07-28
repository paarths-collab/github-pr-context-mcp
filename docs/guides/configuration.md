# Configuration

## Local MCP server

v0.3 retrieves context only. An official local release bundles one product-owned GitHub App Client ID, then authorizes each user through Device Flow. Do not configure an LLM provider or an LLM API key for this server.

```json
"env": {
  "CHROMA_PERSIST_DIR": "/optional/stable/path/chroma_db"
}
```

- `GITHUB_CREDENTIAL_PROFILE` is optional and defaults to `default`. It separates local OS-vault entries when one machine uses multiple GitHub identities.
- `CHROMA_PERSIST_DIR` is optional. Its default is `~/.github-pr-mcp/chroma_db`.
- Do not set `GITHUB_TOKEN`, `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_PRIVATE_KEY`, or `GITHUB_APP_PRIVATE_KEY_PATH`. Local v0.3 ignores/rejects these values to avoid shipping confidential credentials.
- The connected IDE agent provides the reasoning model and its own model configuration.

Restart the MCP client, then call `get_github_connection_status`, `begin_github_authorization`, and `complete_github_authorization`. Install the returned product App URL only on repositories the user chooses. The result is stored in the operating-system credential vault, not in this configuration file or the project database.

`GITHUB_APP_CLIENT_ID` and `GITHUB_APP_SLUG` are maintainer-only overrides for a fork or pre-release build. The official public release gets these two non-secret values from `auth/product_github_app.py`.

See [Free local GitHub App flow](github-app-device-flow.md) for the maintainer registration, user approval, expiry, and disconnect steps.

## Hosted configuration

The current repository has an HTTP entry point and deployment configuration, but multi-user authentication and isolation need release validation before being presented as a production onboarding path. For now, prefer the local mode for a predictable v0.3 workflow.

If operating a controlled deployment, use a dedicated service credential and a persistent `CHROMA_PERSIST_DIR`. Do not reuse the local OS-vault Device Flow design or configure App secrets in a client. Do not add LLM provider settings: this server does not invoke a reasoning provider.

Back to [Quickstart](../quickstart.md)
