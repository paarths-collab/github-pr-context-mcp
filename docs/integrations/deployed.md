# Deployed Integration

Use this when you want a hosted MCP server on Render.

For Gmail-only sign-up and identity separation, see [Gmail-only auth](auth.md).
For full per-IDE configs, see [Client configurations](clients.md).

## Render

Deploy `entrypoints/deployed/server.py` as the start command. Set these environment variables:

- `MCP_TRANSPORT=streamable-http`
- `HOST=0.0.0.0`
- `PORT=10000` or the port Render assigns
- `GITHUB_TOKEN`
- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_API_KEY`
- `CHROMA_PERSIST_DIR=/var/data/chroma_db` or another Render persistent disk mount
- `USAGE_TRACKING_ENABLED=true`
- `USAGE_STATS_PATH=/var/data/usage_stats.json`
- `AUTH_REQUIRED=true`
- `AUTH_REGISTRY_PATH=/var/data/auth_registry.json`
- `MCP_PUBLIC_URL=https://YOUR-SERVICE.onrender.com/mcp`
- `REGISTRATION_SECRET=...`

These LLM and GitHub values are configured by the deployer by default:

- `GITHUB_TOKEN`
- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_API_KEY`

Simple mode: users do not need to set any API keys and can just use bearer auth.

Advanced mode (optional): authenticated users can store their own `github_token` and LLM settings via `/settings`, and those values are used for their requests.

Each user registers a Gmail once and gets a bearer token. The Gmail becomes the namespace, so two different people cannot share the same email without sharing the same token.

If you want the user's storage to stay local instead of hosted, use the local integration instead of Render.
