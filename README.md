# claudekvm — a PiKVM harness for LLM agents

Let an LLM agent (Claude Code, Hermes, OpenClaw, or any MCP-capable agent)
operate a physical computer through a [PiKVM](https://pikvm.org): **capture the
screen** to see what's displayed, and **drive keyboard/mouse over HID** to
interact. Because the agent works from pixels and USB input, it is
platform-agnostic — the target can run Windows, macOS, or Linux, with no agent
installed on it.

Two front-ends over one core:

- a `pikvm` **CLI** for direct use (and for agents that drive a shell);
- an **MCP server** (`pikvm mcp`) exposing the same actions as typed tools,
  with screenshots returned inline.

Dangerous actions (power/reset, destructive typed commands, Ctrl+Alt+Del) are
gated behind a **confirm-token** flow a human must approve, and everything is
written to an audit log.

## Install

```bash
uv venv && uv pip install -e ".[dev]"   # or: pip install -e ".[dev]"
```

## Configure the device

Fastest path — the onboarding wizard:

```bash
pikvm setup                  # prompts for host/user/password, verifies, saves a profile
pikvm profiles               # list configured PiKVMs
pikvm --profile lab status   # target a specific one
```

Under the hood, the endpoint (host/user/ssl/port) comes from, in order,
`--mock`, `PIKVM_*` env vars, then `~/.config/pikvm-agent/config.toml`, which
holds one or more **named profiles** (multiple PiKVMs):

```toml
default = "my_desktop"

[profiles.my_desktop]
host = "192.168.1.50"
user = "admin"
use_ssl = true          # PiKVM ships self-signed certs; TLS verification is off
# port = 443            # optional
# password = "..."      # optional — prefer the OS keyring (see below)

[profiles.lab]
host = "10.0.0.5"
user = "admin"
```

Select a profile with `--profile NAME`, `PIKVM_PROFILE`, or the `default` key.
A legacy flat file (top-level `host = ...`) still works as a single profile. A
single `PIKVM_HOST` env var overrides everything for one-off / CI use.

The **password** is resolved separately so it needn't sit in plaintext, in
order: `PIKVM_PASSWORD` env var → `password` in the TOML → **OS keyring**.
The keyring (GNOME Keyring / KWallet / macOS Keychain / Windows Credential
Manager, via the `keyring` package) is the recommended store:

```bash
pikvm login                  # prompts for the password, verifies, stores in the keyring
pikvm logout                 # removes the stored password
```

With the password in the keyring you can omit the `password` line from the TOML
entirely. If no secret-service daemon is available (e.g. a headless box with no
keyring daemon running), the tool falls back to `PIKVM_PASSWORD` / the TOML
file, and `pikvm login` reports the situation clearly rather than failing
silently.

## CLI

```bash
pikvm status                 # reachability, ATX power LED, keyboard/mouse online
pikvm screenshot -o s.jpg    # capture screen -> JPEG (+ resolution); --ocr for text
pikvm screenshot --crop 750,240,730,590   # native-res region only (cheap, still legible)
pikvm screenshot --settle    # wait until the screen stops changing, then capture
pikvm move 640 400           # move mouse to screen pixels
pikvm click left 640 400     # click (--double for double-click)
pikvm drag 100 100 400 400   # left-drag
pikvm wheel -3               # scroll (negative = down)
pikvm type "uname -a" --enter  # type text then press Enter (type-and-run)
pikvm key "ctrl+alt+t"       # press a combo; also: pikvm key enter
pikvm atx power              # power control (guarded)
pikvm confirm <TOKEN>        # approve a blocked action
pikvm login                  # store the kvmd password in the OS keyring
pikvm logout                 # remove it from the keyring
pikvm setup                  # onboarding wizard: add a profile
pikvm profiles               # list configured PiKVMs
pikvm import registry.json   # bulk-import profiles (e.g. exported from pikonnect)
pikvm pair                   # create a pairing token for the enrollment gateway
pikvm enroll-server          # run the gateway so a companion app can register PiKVMs
pikvm mcp                    # run the MCP server over stdio
```

Global `--profile NAME` selects which configured PiKVM a command targets.

Add `--mock` to any command to target the bundled mock device instead of real
hardware. Exit code `3` means an action was blocked pending confirmation.

## MCP

Register with Claude Code:

```bash
claude mcp add pikvm -- pikvm mcp
```

Tools: `screenshot` (with `crop`/`settle`), `wait_for_stable`, `screen_ocr`,
`system_status`, `mouse_move`, `mouse_click`, `mouse_drag`, `wheel`,
`type_text` (with `submit`), `run_actions`, `press_keys`, `atx_action`,
`confirm_action`. A blocked action returns a token to relay to the human;
call `confirm_action(token)` only after they approve.

### Token-efficient capture

Screenshots dominate token cost (~1,500 per full 1080p frame). To keep it down
without losing legibility: **crop** to a native-resolution region
(`screenshot(crop=[x,y,w,h])`) rather than downscaling; use `screen_ocr` for
text-only reads; use **`settle`** / `wait_for_stable` to capture once the screen
stops changing (no blind delays); and batch trusted input with `run_actions` /
`type_text(submit=True)` to cut round-trips.

### Hosting the MCP server

Local (default): the client spawns it over stdio — `claude mcp add pikvm -- pikvm mcp`.

Remote (drive from a cloud/off-LAN agent): run it as an HTTP service on an
always-on box that shares the PiKVM's LAN, and reach it over a private mesh:

```bash
pikvm mcp --http --bind 0.0.0.0 --port 8000     # serves at http://<host>:8000/mcp
```

**Never expose this on the public internet** — it can power-cycle and type into
the target. Put it behind Tailscale/WireGuard with the confirm-token guardrails
as the backstop. On a headless host, keyring won't have a daemon, so supply
credentials via `PIKVM_*` env vars or the TOML file there.

### Enrollment gateway (register PiKVMs from a companion app)

Instead of copying config files, a companion app (e.g. pikonnect) can push new
PiKVM profiles to the server over the network:

```bash
pikvm pair                                    # prints a one-time pairing token
pikvm enroll-server --bind 0.0.0.0 --port 8099   # authenticated gateway
```

The app sends the token as a Bearer credential to `POST /enroll`; imported
profiles are usable immediately (no restart). The endpoint is credential-bearing
— **keep it on a private mesh, never public**. Contract and app-side spec:
`docs/pikonnect-export.md` and `docs/pikonnect-enroll-requirement.md`.

## Architecture

`docs/architecture.html` is a standalone, diagrammed architecture document
(system overview, component/data-flow diagrams, module reference, recommended
models, and the security model). Open it in a browser.

## Agent playbook

`skills/pikvm-remote/SKILL.md` is a drop-in Claude Code skill describing the
screenshot → decide → act → verify loop, per-OS "check system state" recipes,
and the guardrail protocol.

## Guardrails

Blocked unless confirmed with a one-time token (10-minute expiry, printed for
a human to approve):

- typed destructive commands — `shutdown`, `reboot`, `poweroff`, `rm -rf`,
  `mkfs`, `dd if=`, `format`, `Stop-Computer`, `taskkill /f`, … (cross-platform);
- an `Enter` that would execute a dangerous command already typed (a rolling
  buffer of recent keystrokes catches the type-then-Enter split);
- the Ctrl+Alt+Del power chord;
- all ATX power actions.

Audit log and guard state live under `~/.local/state/pikvm-agent/`.

Residual risk: an agent could type a dangerous command one character at a time
via `key` events to dodge text scanning. The Enter-evasion buffer mitigates the
common case, and the audit log records everything; treat the confirm-token
prompt as the real safety boundary and keep a human in the loop for power
actions.

## Development

```bash
pytest                       # full suite against the bundled mock device
python -m mock.mock_kvmd      # run the mock kvmd standalone (records HID to JSONL)
```

The mock (`mock/mock_kvmd.py`) implements the kvmd API surface the harness uses
— auth, info, HID, ATX, snapshot, MJPEG stream, `/api/ws` — and records every
HID/ATX event to a JSONL file so tests assert exact event sequences.

## How it talks to the PiKVM

- **Auth:** `X-KVMD-User` / `X-KVMD-Passwd` headers (HTTP + WebSocket), with a
  cookie-login fallback. Self-signed TLS accepted.
- **Screen:** `GET /api/streamer/snapshot` (JPEG; `?ocr=1` for text). kvmd
  suspends the streamer when no client is attached, so the harness briefly
  holds `/api/ws` open to wake it, and falls back to a single MJPEG `/stream`
  frame if needed.
- **HID:** mouse over the `/api/ws` WebSocket with absolute signed coordinates
  `[-32768, 32767]` (`kvm = int(px/size*65535 - 32768)`); keyboard via
  `/api/hid/print` (bulk) or per-key WS events.
- **Power:** `GET /api/atx`, `POST /api/atx/click?button=…`.
