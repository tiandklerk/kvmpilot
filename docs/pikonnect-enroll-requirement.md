# Requirement: "Export to Gateway" in pikonnect

**For the engineer/agent working on the pikonnect Android app.**

## Goal

Let a user push a registered PiKVM profile from pikonnect directly to the
kvmpilot **enrollment gateway** running on their always-on server, over the
local network — no file export/import, all in the UI. This mirrors how the app
already sends an SSH connection to that server.

The gateway (server side) is **already built** in kvmpilot. This requirement
covers only the app side: a settings screen to pair with a gateway, and an
"Export to Gateway" action on KVM profiles.

## Server side you're talking to (already implemented)

The user runs, on the server:

```
pikvm pair                                   # prints a one-time pairing token
pikvm enroll-server --bind 0.0.0.0 --port 8099   # starts the gateway
```

HTTP API (JSON):

| Method | Path        | Auth                     | Body / result |
|--------|-------------|--------------------------|---------------|
| GET    | `/health`   | none                     | `{"ok":true,"service":"pikvm-enroll"}` — connectivity check |
| POST   | `/enroll`   | `Authorization: Bearer <token>` | body = registry JSON (below) → `200 {"ok":true,"imported":["name",…]}` |
| GET    | `/profiles` | `Authorization: Bearer <token>` | `{"ok":true,"profiles":[{name,host,user,use_ssl,port}],"default":"name"}` (no passwords) |

Errors: `401` (missing/invalid token), `400 {"ok":false,"error":"…"}` (bad
payload). Import is idempotent per profile `name` (re-export updates it).

## Registry JSON (POST /enroll body)

```json
{
  "version": 1,
  "default": "my_desktop",
  "profiles": [
    {
      "name": "my_desktop",
      "host": "192.168.1.50",
      "user": "admin",
      "use_ssl": true,
      "port": 443,
      "password": "optional"
    }
  ]
}
```

- `name` (letters/digits/`.`/`_`/`-`, ≤64) and `host` are required.
- `user` defaults to `admin`, `use_ssl` to `true`, `port` to the scheme default.
- `password` optional — see security below. When present the gateway stores it
  in the server's OS keyring; when absent the user runs `pikvm login` there.
- Sending one profile (the tapped KVM) or many (export-all) both use this shape.
  Map pikonnect's stored profile fields directly onto it.

## App-side work

1. **Gateway settings** (new screen, alongside the existing SSH server config):
   - Fields: *Gateway URL* (e.g. `http://192.168.1.10:8099`) and *Pairing token*.
   - "Test connection" → `GET /health`, show success/failure.
   - Persist the token in **encrypted storage** (Android Keystore /
     EncryptedSharedPreferences), never plain SharedPreferences or logs.
   - (Optional, nice-to-have) pair by scanning a QR containing `{url, token}`.

2. **"Export to Gateway" action** on a KVM profile (and an "Export all"):
   - Build the registry JSON from the profile(s).
   - `POST /enroll` with `Authorization: Bearer <token>` and
     `Content-Type: application/json`.
   - On `200`: toast/snackbar "Registered N profile(s) on the gateway."
   - On `401`: "Pairing token rejected — re-pair with the gateway." (prompt to
     re-enter the token).
   - On `400`: surface `error` from the body.
   - On network failure: "Couldn't reach the gateway" with retry.

3. **Password toggle**: a per-export switch "Send password to gateway"
   (default your call, but recommend **on** for frictionless setup *only when the
   gateway URL is https or the user confirms the network is trusted*). If off,
   omit `password`; the profile still enrolls and the user completes it with
   `pikvm login` on the server.

## Security requirements

- The payload carries device credentials → treat the token and any passwords as
  secrets: encrypted storage, never logged, TLS or a trusted mesh in transit.
- If the gateway URL is plain `http://` and not on a mesh (Tailscale/WireGuard),
  warn before sending a password.
- Do not retry a `401` automatically with the same token; ask the user to re-pair.

## Out of scope

- The app is **not** an MCP server and does not drive KVMs itself here; it only
  registers them with the gateway. (Interactive in-app driving, if ever wanted,
  is a separate feature.)
- No changes to how pikonnect talks to kvmd directly.

## Acceptance

- With `pikvm enroll-server` running and a valid token, tapping "Export to
  Gateway" on a KVM makes it appear in `pikvm profiles` on the server, and the
  agent can immediately `pikvm --profile <name> status` it.
- A wrong/blank token yields a clear "re-pair" error, not a silent failure.
