# pikonnect → harness onboarding contract

The [pikonnect](https://github.com/tiandklerk/pikonnect) Android app is where you
register your PiKVMs (host, user, password, TLS, port live in its Room DB). This
harness runs the MCP server on an always-on box. Rather than make the phone the
server (Android kills background services, roams off-LAN, and would duplicate the
guardrail layer), pikonnect acts as the **registry / onboarding source**: it
exports its profiles and the harness imports them with `pikvm import`.

## The export format

pikonnect writes a JSON document:

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
      "password": "optional — omit to keep it off the wire"
    },
    { "name": "lab", "host": "10.0.0.5", "user": "admin" }
  ]
}
```

- `name` and `host` are required per profile; `user` defaults to `admin`,
  `use_ssl` to `true`, `port` to the scheme default.
- `password` is **optional**. Include it and the harness stores it in the OS
  keyring on import; omit it (recommended for transfers you don't fully trust)
  and the harness imports the endpoint only — you then run
  `pikvm login --profile <name>` on the harness box.
- `default` (optional) names the profile to mark as default.

## How the app gets it to the harness

Recommended primary path — **file export + import** (simple, offline, works on a
headless host, you control the transfer):

1. In pikonnect: *Export profiles* → produces `pikvm-registry.json`.
2. Move it to the harness box (share sheet → save, `scp`, a synced folder…).
3. On the harness box: `pikvm import pikvm-registry.json`.

Quick single-device path — **QR code** (where the harness box has a camera or
you can paste): pikonnect shows a QR of one profile's JSON; decode and feed it to
`pikvm import`. Good for adding one KVM without a file transfer.

Guidance:
- Prefer the **password-less** export and `pikvm login` on the harness, so the
  secret is entered once, locally, into the keyring — not carried in a file.
- If you do include passwords, treat the export file as a secret: transfer it
  over a trusted channel and delete it after import.
- Re-importing is idempotent per `name` — it updates the existing profile.

## Why not run the MCP server on the phone?

- Android aggressively kills background services and throttles in Doze; a
  phone-hosted server is unreliable for unattended agents.
- The phone's IP roams and it leaves the LAN; the endpoint moves.
- It would fork the security-critical tool + guardrail layer into Kotlin, to be
  kept in sync with this harness.

Keep the server on the always-on box; let pikonnect do what it's uniquely good
at — being the place your KVMs are registered. (An in-app, foreground-only
interactive driver is a reasonable separate feature; unattended hosting is not.)
