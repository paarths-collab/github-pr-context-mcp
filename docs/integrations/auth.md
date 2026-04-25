# Gmail-Only Auth

The deployed Render server uses Gmail-only bearer tokens so the server can identify each user and keep their data isolated.

## How identity works

- Each Gmail address can be registered once.
- The server issues one bearer token per Gmail address.
- That Gmail address becomes the storage namespace for that user.
- If the same Gmail is submitted again, registration is rejected.

## Why this is safer

- The server does not trust arbitrary usernames.
- Token hashes are stored, not plain tokens.
- Token comparison uses constant-time checks.
- Tool input still goes through strict validation.
- Repo and namespace names are normalized before storage access.

## Register a Gmail

Send a POST request to `/register` on your Render server.

```json
{
  "email": "yourname@gmail.com",
  "invite_secret": "YOUR_SHARED_REGISTRATION_SECRET"
}
```

This is the simple mode and is enough for most users.

If registration is allowed, the server returns:

```json
{
  "email": "yourname@gmail.com",
  "token": "...",
  "authorization": "Bearer ...",
  "namespace": "yourname@gmail.com",
  "settings": {}
}
```

## Optional advanced settings

Users can optionally provide their own API settings, but this is not required.

At registration time, include `settings` only if needed:

```json
{
  "email": "yourname@gmail.com",
  "invite_secret": "YOUR_SHARED_REGISTRATION_SECRET",
  "settings": {
    "github_token": "ghp_...",
    "llm_provider": "groq",
    "llm_model": "llama-3.3-70b-versatile",
    "llm_api_key": "..."
  }
}
```

You can also update settings later with `PUT /settings`:

```json
{
  "settings": {
    "llm_provider": "cerebras",
    "llm_model": "llama3.1-8b"
  }
}
```

To inspect current settings (masked), call `GET /settings` with your bearer token.

## Use the token

Add the returned bearer token to your MCP client config as an `Authorization` header.

For remote clients, the header should look like:

```json
"Authorization: Bearer YOUR_TOKEN"
```

## Recommended env vars on Render

- `AUTH_REQUIRED=true`
- `REGISTRATION_SECRET=...`
- `AUTH_REGISTRY_PATH=/var/data/auth_registry.json`
- `MCP_PUBLIC_URL=https://YOUR-SERVICE.onrender.com/mcp`

## Operational rule

- One Gmail = one namespace = one registered token.
- If you want a stricter workflow, keep `REGISTRATION_SECRET` private and only share it with approved users.
- If you need Google sign-in instead of token registration, that can be added later on top of this structure.
