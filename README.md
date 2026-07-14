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

The connection endpoint (host/user/ssl/port) comes from, in order, `--mock`,
`PIKVM_*` env vars, then `~/.config/pikvm-agent/config.toml`:

```toml
host = "192.168.1.50"
user = "admin"
use_ssl = true          # PiKVM ships self-signed certs; TLS verification is off
# port = 443            # optional
# password = "..."      # optional — prefer the OS keyring (see below)
```

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
pikvm move 640 400           # move mouse to screen pixels
pikvm click left 640 400     # click (--double for double-click)
pikvm drag 100 100 400 400   # left-drag
pikvm wheel -3               # scroll (negative = down)
pikvm type "hello world"     # type text (server-side keymap)
pikvm key "ctrl+alt+t"       # press a combo; also: pikvm key enter
pikvm atx power              # power control (guarded)
pikvm confirm <TOKEN>        # approve a blocked action
pikvm login                  # store the kvmd password in the OS keyring
pikvm logout                 # remove it from the keyring
pikvm mcp                    # run the MCP server over stdio
```

Add `--mock` to any command to target the bundled mock device instead of real
hardware. Exit code `3` means an action was blocked pending confirmation.

## MCP

Register with Claude Code:

```bash
claude mcp add pikvm -- pikvm mcp
```

Tools: `screenshot`, `screen_ocr`, `system_status`, `mouse_move`,
`mouse_click`, `mouse_drag`, `wheel`, `type_text`, `press_keys`, `atx_action`,
`confirm_action`. A blocked action returns a token to relay to the human;
call `confirm_action(token)` only after they approve.

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
