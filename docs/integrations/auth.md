# Hosted authentication status

The codebase contains identity and bearer-token components for a future hosted deployment, but the current v0.3 route set does not provide a complete, documented self-service registration flow. This document intentionally does not instruct users to call a nonexistent registration endpoint.

## Current recommendation

Use the local stdio integration while v0.3 is being stabilized. The local server uses the product-owned GitHub App through Device Flow, keeps its index on the local machine, and stores the GitHub credential in the operating-system vault. See [Free local GitHub App flow](../guides/github-app-device-flow.md).

## Before enabling multi-user hosting

Treat these as release requirements:

1. Expose and test a registration or administrator-provisioning flow.
2. Verify bearer-token scope validation on every protected route and tool.
3. Use a tenant-bound encrypted credential manager; never reuse the local OS-vault or plaintext SQLite-settings approach.
4. Test that each tenant can independently index, query, refresh, list, and delete the same repository without accessing another tenant’s data.
5. Document token rotation, revocation, and recovery.

Back to [Integrations](index.md)
