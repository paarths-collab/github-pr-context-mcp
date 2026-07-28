# Hosted deployment status

The repository includes a streamable-HTTP entry point and `render.yaml`, but v0.3’s hosted multi-user enrollment and storage-isolation path is not yet validated for a production release.

Use the [local integration](local.md) for the supported v0.3 workflow.

## Controlled deployment notes

For a controlled infrastructure experiment, the existing Render configuration can supply:

- `HOST` and `PORT`
- `CHROMA_PERSIST_DIR=/var/data/chroma_db` on the persistent disk

It is not an enabled GitHub retrieval deployment: local Device Flow tools correctly return `unsupported` in hosted mode, and this release deliberately does not accept a global `GITHUB_TOKEN`. The server does not need an LLM provider, model name, or LLM API key. The client IDE agent performs the final reasoning.

The new local GitHub App Device Flow is deliberately **not** a hosted credential design: an operating-system vault belongs to one developer's machine, not to a shared web service. Never put `GITHUB_APP_CLIENT_SECRET` or a private key in an MCP client configuration or Render YAML.

Before offering hosted access to multiple people, implement and test tenant registration, GitHub App installation callbacks/webhooks, authorization, short-lived installation credentials in an encrypted tenant-bound vault/KMS, refresh locking, revocation, namespace-specific indexing, querying, and deletion. Do not infer those guarantees from configuration names alone.

Back to [Integrations](index.md)
