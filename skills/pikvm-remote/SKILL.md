---
name: pikvm-remote
description: Operate a physical computer through a PiKVM — capture its screen and drive keyboard/mouse to check system state or perform a task, platform-agnostically (Windows/macOS/Linux). Use when asked to "check the system", "see what's on screen", "log into the box", or interact with a host that has no SSH/agent, only a PiKVM.
---

# Operating a computer through a PiKVM

You control the target purely through what a KVM sees and does: pixels in,
USB keyboard/mouse out. Nothing runs on the target. Use the `pikvm` CLI
(or the equivalent MCP tools). Add `--mock` to any command to rehearse
against the local mock device.

## The core loop — never fly blind

1. **Look.** `pikvm screenshot -o /tmp/s.jpg` then open the image. It prints
   the resolution — coordinates you pass are in those screen pixels.
2. **Decide** one small action from what you see.
3. **Act.** `move` / `click` / `type` / `key`.
4. **Verify.** Prefer `pikvm screenshot --settle` (or the `wait_for_stable`
   tool) so you capture once the screen has stopped changing, instead of
   guessing a delay. Confirm the effect before the next action. Do not chain
   several blind inputs; the target may lag, focus may be wrong, a dialog may
   have appeared.

### Keeping it cheap without going blind

Screenshots dominate token cost, so spend them well — but **don't reach for
`--crop` by default.** A full-screen frame is your context; cropping is a
keyhole. Guidance:
- **Full frame** to orient, navigate, or whenever you're unsure where things
  are. Do **not** downscale — it makes text illegible and barely saves tokens.
- **`--crop x,y,w,h`** only to re-read a region you have *already located* (a
  dialog, a status line) — native resolution, cheap, still legible. If you'd
  need several crops to find something, take one full frame instead.
- **`screen_ocr`** for text-only reads (terminal output, a value) — far cheaper
  than an image, but it can't see icons or tell you where to click.
- **Batch trusted input** with `type "cmd" --enter` or the `run_actions` tool so
  a known sub-sequence costs one round-trip, then verify with one screenshot.
Coordinates always refer to the full screen, even after a crop.

## Choosing a device

If several PiKVMs are configured (`pikvm profiles`), pass `--profile NAME` (or
set a default). The MCP server targets whichever profile it was started with.

## First, orient (check system state)

Run `pikvm status` — reports device reachability, ATX power LED, and whether
the keyboard/mouse HID are online. If `mouse: OFFLINE`, moves/clicks won't
land until the host enumerates the USB mouse (re-plug OTG / reboot host);
tell the user rather than clicking into the void.

Then `pikvm screenshot` and identify the OS from what's on screen:
- **Windows** — taskbar at the bottom, Start button bottom-left.
- **macOS** — menu bar top, Dock usually at the bottom, Apple logo top-left.
- **Linux** — varies by desktop (GNOME top bar + dock, KDE panel, a login
  greeter, or a bare TTY with a text prompt).

## Navigating to system info (platform-agnostic recipes)

- **Windows:** `pikvm key meta` (opens Start), `pikvm type "winver"`,
  `pikvm key enter` → version dialog; or Settings → System → About.
- **macOS:** click the Apple menu (top-left, ~x=15,y=12) → "About This Mac".
- **Linux (GNOME):** open Settings from the dock/app grid → About; or open a
  terminal and read state with **read-only** commands (`uname -a`, `uptime`,
  `df -h`, `free -h`, `top -bn1 | head`). Never run state-changing commands
  as part of "checking".

## Typing and keys

- `pikvm type "text"` types a string (uses the server-side keymap).
- `pikvm key "ctrl+alt+t"` presses a combo; `pikvm key enter` submits.
- Coordinates: `pikvm click 640 400`, `pikvm move 100 200`,
  `pikvm drag 100 100 400 400`, `pikvm wheel -3`.

## Guardrails — you cannot bypass them, and shouldn't try

Dangerous actions are **blocked** and exit with code 3 and a token:
- typing destructive commands (shutdown, reboot, rm -rf, mkfs, dd, format,
  Stop-Computer, …) — caught in the text *and* when a later `key enter`
  would execute a dangerous command already typed;
- the Ctrl+Alt+Del power chord;
- any `pikvm atx power|power_long|reset`.

When you hit exit code 3: **stop, show the user the exact blocked action and
the token, and ask for explicit approval.** Only after they approve, run
`pikvm confirm <TOKEN>` (or re-run with `--confirm <TOKEN>`). Never invent or
reuse a token to push an action through on your own. Every action is written
to an audit log regardless.

## MCP equivalent

The same capabilities are exposed as MCP tools (`pikvm mcp`): `screenshot`
returns the image inline, plus `system_status`, `mouse_move`, `mouse_click`,
`mouse_drag`, `wheel`, `type_text`, `press_keys`, `atx_action`,
`confirm_action`, `screen_ocr`. Guardrails behave identically — a blocked
tool returns a token to relay to the human.
