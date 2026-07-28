# Integrations

## Local MCP (recommended for v0.3)

Run the server as a local stdio process from your IDE. Chroma data stays on the machine that runs the server, the IDE agent owns all reasoning, and GitHub access uses the product-owned GitHub App through Device Flow with a credential stored in the OS vault.

- [Local setup](local.md)
- [Client configuration examples](clients.md)
- [Configuration reference](../guides/configuration.md)
- [Free local GitHub App flow](../guides/github-app-device-flow.md)

## Hosted MCP

The repository includes an HTTP entry point and Render configuration, but the hosted multi-user enrollment and isolation path is not yet a release-ready v0.3 workflow. Do not use it for a production multi-tenant deployment without completing and testing that work.

- [Hosted deployment notes](deployed.md)
- [Authentication status](auth.md)
