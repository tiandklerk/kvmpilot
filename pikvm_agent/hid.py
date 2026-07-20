"""HID input: mouse (absolute, over the kvmd WebSocket) and keyboard.

Coordinates are given in target-screen pixels and mapped to kvmd's
signed absolute range [-32768, 32767] (same math as the PiKVM web UI):
    kvm = int(px / size * 65535 - 32768)
The target resolution comes from the last screenshot (cached in the
state dir) so mouse commands don't need a capture each time.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from contextlib import contextmanager

from . import guard, keymap
from .client import HidWebSocket, PiKvmClient, PiKvmError

# Human-like cursor motion: off by default (instant absolute jumps are faster for
# ordinary UI work). Turn on per session — e.g. for inputs that watch for robotic
# input — via the env var, the MCP `set_mouse_humanize` tool, or a per-call arg.
HUMANIZE = os.environ.get("PIKVM_HUMANIZE_MOUSE", "").lower() in ("1", "true", "yes")


@contextmanager
def _ws_for(client: PiKvmClient, ws: HidWebSocket | None):
    """Yield a socket for one action. If `ws` is given (a borrowed persistent
    session socket), use it and leave it open; otherwise open a fresh one and
    close it on exit — preserving the original one-shot CLI behaviour."""
    if ws is not None:
        yield ws
    else:
        with client.open_ws() as fresh:
            yield fresh

KEY_DELAY = 0.02   # seconds between HID events, mirroring pikonnect's pacing
FLUSH_DELAY = 0.1  # let kvmd process the final event before the socket closes


# -- coordinates --------------------------------------------------------------

def to_kvm(px: float, size: int) -> int:
    return max(-32768, min(32767, int(px / size * 65535 - 32768)))


def pixel_to_kvm(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    return to_kvm(x, width), to_kvm(y, height)


def get_resolution(client: PiKvmClient) -> tuple[int, int]:
    """Cached resolution from the last screenshot, else capture one now."""
    from . import screen

    if screen.RESOLUTION_CACHE.exists():
        data = json.loads(screen.RESOLUTION_CACHE.read_text())
        return data["width"], data["height"]
    snap = screen.capture(client)
    return snap.width, snap.height


# -- mouse --------------------------------------------------------------------

MOUSE_SWITCH_TIMEOUT = 8.0  # seconds for the gadget to re-enumerate after a switch


def ensure_absolute_mouse(client: PiKvmClient, auto_switch: bool = False) -> str:
    """Verify the device is in kvmd's absolute pointer mode, which pixel-
    coordinate actions require — WITHOUT changing anything by default.

    Switching the mouse output re-enumerates the USB mouse gadget, and on some
    hosts a re-enumeration is not cleanly re-grabbed: the mouse drops offline
    (seen live — repeated switching left an Ubuntu host unable to enumerate the
    gadget, while the never-switched keyboard stayed online). So normal mouse
    actions NEVER switch; if the device is in a relative output they raise with
    guidance. Pass auto_switch=True only from an explicit user action (the CLI
    `mouse-mode` command) to actually perform the switch.

    Returns "absolute" (already fine), or "switched:<output>" when auto_switch
    performed a change. The positive result is cached per client, so within one
    process the check costs a single request.
    """
    if getattr(client, "_mouse_absolute_ok", False):
        return "absolute"
    mouse = client.hid_state().get("mouse", {})
    if mouse.get("absolute"):
        client._mouse_absolute_ok = True
        return "absolute"

    outputs = mouse.get("outputs", {})
    active = outputs.get("active")
    available = outputs.get("available") or []
    # kvmd's relative outputs carry a _rel suffix (usb_rel); anything else is absolute
    candidates = [o for o in available if o and not o.endswith("_rel") and o != "disabled"]
    if not candidates:
        raise PiKvmError(
            f"mouse is in relative mode (output {active!r}) and this device offers no "
            f"absolute output (available: {available}); pixel-coordinate mouse actions "
            "cannot work — set an absolute mouse output in the PiKVM web UI"
        )
    target = "usb" if "usb" in candidates else candidates[0]

    if not auto_switch:
        raise PiKvmError(
            f"mouse is in relative mode (output {active!r}); pixel-coordinate actions need "
            "absolute positioning. Switch it explicitly with `pikvm mouse-mode` (or in the "
            "PiKVM web UI) — the harness will not switch on its own because re-enumerating "
            "the USB mouse gadget can drop it offline on some hosts."
        )

    client.set_mouse_output(target)
    deadline = time.time() + MOUSE_SWITCH_TIMEOUT
    while time.time() < deadline:
        m = client.hid_state().get("mouse", {})
        if m.get("absolute") and m.get("online"):
            client._mouse_absolute_ok = True
            guard.audit("mouse_output", "executed", f"switched to {target} (absolute)")
            return f"switched:{target}"
        time.sleep(0.3)
    raise PiKvmError(
        f"switched mouse output to {target!r} but it did not come back online in "
        f"absolute mode within {MOUSE_SWITCH_TIMEOUT:.0f}s"
    )


def _humanized_points(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """A slightly-curved, eased, jittered path of pixel points from (x0,y0) to
    (x1,y1) — so cursor motion looks like a hand, not an instant teleport."""
    dist = math.hypot(x1 - x0, y1 - y0)
    steps = max(6, min(40, int(dist / 25)))
    # bow the path sideways so it isn't a dead-straight ruler line
    nx, ny = -(y1 - y0), (x1 - x0)
    norm = math.hypot(nx, ny) or 1.0
    bow = random.uniform(-1, 1) * min(40.0, dist * 0.15)
    cx, cy = (x0 + x1) / 2 + nx / norm * bow, (y0 + y1) / 2 + ny / norm * bow
    pts: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        e = (i / steps) ** 2 * (3 - 2 * (i / steps))  # smoothstep ease-in-out
        px = (1 - e) ** 2 * x0 + 2 * (1 - e) * e * cx + e ** 2 * x1
        py = (1 - e) ** 2 * y0 + 2 * (1 - e) * e * cy + e ** 2 * y1
        pts.append((px + random.uniform(-1.5, 1.5), py + random.uniform(-1.5, 1.5)))
    pts[-1] = (x1, y1)  # land exactly on target
    return pts


def _move_to(client: PiKvmClient, sock: HidWebSocket, x: float, y: float,
             w: int, h: int, human: bool) -> None:
    """Position the cursor at pixel (x,y): one absolute jump, or a human-like path."""
    if human:
        x0, y0 = getattr(client, "_last_mouse_px", None) or (w / 2, h / 2)
        for px, py in _humanized_points(x0, y0, x, y):
            sock.mouse_move(*pixel_to_kvm(px, py, w, h))
            time.sleep(random.uniform(0.006, 0.02))
    else:
        sock.mouse_move(*pixel_to_kvm(x, y, w, h))
    client._last_mouse_px = (x, y)


def _use_human(human: bool | None) -> bool:
    return HUMANIZE if human is None else human


def mouse_move(client: PiKvmClient, x: float, y: float, ws: HidWebSocket | None = None,
               human: bool | None = None) -> None:
    ensure_absolute_mouse(client)
    w, h = get_resolution(client)
    with _ws_for(client, ws) as sock:
        _move_to(client, sock, x, y, w, h, _use_human(human))
        time.sleep(FLUSH_DELAY)


CLICK_LABEL_CROP = (240, 90)  # (w, h) region OCR'd around a click point when check_label=True


def _check_click_label(client: PiKvmClient, x: float, y: float, confirm_token: str | None) -> None:
    """OCR a small crop around (x, y) and gate the click if it reads as a
    destructive control. Mouse actions carry no text of their own to scan, so
    this is opt-in (costs an extra capture) rather than the default — see
    click_guard.py and DANGEROUS_CLICK_LABELS."""
    from . import click_guard, screen

    cw, ch = CLICK_LABEL_CROP
    box = (max(0, int(x - cw / 2)), max(0, int(y - ch / 2)), cw, ch)
    snap = screen.capture(client, crop=box)
    text = click_guard.read_click_region(snap.jpeg)  # raises click_guard.OcrUnavailable, not swallowed
    danger = guard.scan_click_label(text)
    if danger is not None:
        guard.require_confirmation(
            f"click:{danger.reason} @ ({int(x)},{int(y)})", danger.reason, confirm_token)


def click(
    client: PiKvmClient,
    button: str = "left",
    x: float | None = None,
    y: float | None = None,
    double: bool = False,
    ws: HidWebSocket | None = None,
    human: bool | None = None,
    check_label: bool = False,
    confirm_token: str | None = None,
) -> None:
    if button not in ("left", "right", "middle"):
        raise PiKvmError(f"unknown mouse button {button!r}")
    if check_label and x is not None and y is not None:
        _check_click_label(client, x, y, confirm_token)
    ensure_absolute_mouse(client)
    w, h = get_resolution(client)
    hu = _use_human(human)
    with _ws_for(client, ws) as sock:
        if x is not None and y is not None:
            _move_to(client, sock, x, y, w, h, hu)
            time.sleep(random.uniform(0.05, 0.14) if hu else KEY_DELAY)  # settle before pressing
        for _ in range(2 if double else 1):
            sock.mouse_button(button, True)
            time.sleep(random.uniform(0.04, 0.11) if hu else KEY_DELAY)  # press dwell
            sock.mouse_button(button, False)
            time.sleep(KEY_DELAY)
        time.sleep(FLUSH_DELAY)


def drag(client: PiKvmClient, x1: float, y1: float, x2: float, y2: float, steps: int = 12,
         ws: HidWebSocket | None = None, human: bool | None = None) -> None:
    ensure_absolute_mouse(client)
    w, h = get_resolution(client)
    hu = _use_human(human)
    with _ws_for(client, ws) as sock:
        _move_to(client, sock, x1, y1, w, h, hu)
        time.sleep(KEY_DELAY)
        sock.mouse_button("left", True)
        time.sleep(KEY_DELAY)
        for i in range(1, steps + 1):
            t = i / steps
            jx = random.uniform(-1.5, 1.5) if hu else 0.0
            jy = random.uniform(-1.5, 1.5) if hu else 0.0
            sock.mouse_move(*pixel_to_kvm(x1 + (x2 - x1) * t + jx, y1 + (y2 - y1) * t + jy, w, h))
            time.sleep(random.uniform(0.01, 0.03) if hu else KEY_DELAY)
        sock.mouse_button("left", False)
        client._last_mouse_px = (x2, y2)
        time.sleep(FLUSH_DELAY)


def wheel(client: PiKvmClient, dy: int, dx: int = 0, ws: HidWebSocket | None = None) -> None:
    ensure_absolute_mouse(client)
    with _ws_for(client, ws) as sock:
        sock.mouse_wheel(dx, dy)
        time.sleep(FLUSH_DELAY)


# -- keyboard -----------------------------------------------------------------

def type_text(client: PiKvmClient, text: str, force_ws: bool = False,
              confirm_token: str | None = None, submit: bool = False,
              ws: HidWebSocket | None = None) -> str:
    """Type text on the target. Prefers server-side /api/hid/print (layout-
    aware, fast); falls back to per-key WebSocket events. Returns the path
    used ("print" or "ws"). Dangerous command text is gated by guardrails.

    submit=True presses Enter right after typing, in the same call — the common
    "type a command and run it" compound. The danger check runs on `text`, so a
    dangerous command is gated once (at the type), not twice.
    """
    danger = guard.scan_text(text)
    if danger is not None:
        guard.require_confirmation(f"type:{text}", danger.reason, confirm_token)
    guard.record_typed(text)
    guard.audit(f"type:{len(text)}chars", "executed", text[:80])

    path = "ws"
    if not force_ws:
        try:
            client.hid_print(text)
            path = "print"
        except PiKvmError:
            path = "ws"
    if path == "ws":
        _type_text_ws(client, text, ws=ws)

    if submit:
        # `text` was already gated above; clear the buffer so the Enter isn't
        # re-flagged by the type-then-Enter evasion check.
        guard.clear_typed_buffer()
        with _ws_for(client, ws) as sock:
            sock.key("Enter", True)
            time.sleep(KEY_DELAY)
            sock.key("Enter", False)
            time.sleep(FLUSH_DELAY)
        guard.audit("key:Enter", "executed", "submit after type")
    return path


def _type_text_ws(client: PiKvmClient, text: str, ws: HidWebSocket | None = None) -> None:
    with _ws_for(client, ws) as sock:
        for c in text:
            mapped = keymap.from_char(c)
            if mapped is None:
                raise PiKvmError(f"cannot type character {c!r} via key events")
            if mapped.shift:
                sock.key("ShiftLeft", True)
                time.sleep(KEY_DELAY)
            sock.key(mapped.key, True)
            time.sleep(KEY_DELAY)
            sock.key(mapped.key, False)
            time.sleep(KEY_DELAY)
            if mapped.shift:
                sock.key("ShiftLeft", False)
                time.sleep(KEY_DELAY)
        time.sleep(FLUSH_DELAY)


def press_combo(client: PiKvmClient, combo: str, confirm_token: str | None = None,
                ws: HidWebSocket | None = None) -> list[str]:
    """Press a key combo like "ctrl+alt+del": press in order, release in
    reverse. Returns the kvmd key codes used. Power chords and an Enter that
    would execute a dangerous typed command are gated by guardrails."""
    keys = keymap.parse_combo(combo)

    danger = guard.scan_combo(keys)
    if danger is None and keys == ["Enter"]:
        danger = guard.enter_would_execute_danger()
    if danger is not None:
        guard.require_confirmation(f"key:{combo}", danger.reason, confirm_token)

    if "Enter" in keys:
        guard.clear_typed_buffer()  # command line submitted; reset the buffer
    guard.audit(f"key:{combo}", "executed", "+".join(keys))

    with _ws_for(client, ws) as sock:
        for k in keys:
            sock.key(k, True)
            time.sleep(KEY_DELAY)
        for k in reversed(keys):
            sock.key(k, False)
            time.sleep(KEY_DELAY)
        time.sleep(FLUSH_DELAY)
    return keys


# -- compound actions ---------------------------------------------------------

def run_sequence(client: PiKvmClient, actions: list[dict],
                 confirm_token: str | None = None,
                 ws: HidWebSocket | None = None) -> list[str]:
    """Run a list of input steps in order, so an agent spends one round-trip on
    a trusted sub-sequence instead of one per step. Each action is a dict with
    an "action" key: move{x,y} | click{button?,x?,y?,double?} | drag{x1,y1,x2,y2}
    | wheel{dy} | type{text,submit?} | key{combo}. Guardrails apply per step; a
    blocked step raises GuardError and stops the sequence there.

    All steps share one WebSocket (the borrowed `ws`, or one opened here), so a
    batch of N clicks pays a single connection handshake instead of N — the main
    latency win for multi-target UIs like image-grid captchas.
    """
    results: list[str] = []
    with _ws_for(client, ws) as sock:
        for i, a in enumerate(actions):
            kind = a.get("action")
            if kind == "move":
                mouse_move(client, a["x"], a["y"], ws=sock, human=a.get("human"))
                results.append(f"move ({a['x']},{a['y']})")
            elif kind == "click":
                click(client, a.get("button", "left"), a.get("x"), a.get("y"),
                      double=a.get("double", False), ws=sock, human=a.get("human"),
                      check_label=a.get("check_label", False), confirm_token=confirm_token)
                results.append(f"{a.get('button', 'left')} click")
            elif kind == "drag":
                drag(client, a["x1"], a["y1"], a["x2"], a["y2"], ws=sock, human=a.get("human"))
                results.append("drag")
            elif kind == "wheel":
                wheel(client, a["dy"], ws=sock)
                results.append(f"wheel {a['dy']}")
            elif kind == "type":
                type_text(client, a["text"], confirm_token=confirm_token,
                          submit=a.get("submit", False), ws=sock)
                results.append(f"type {len(a['text'])} chars")
            elif kind == "key":
                press_combo(client, a["combo"], confirm_token=confirm_token, ws=sock)
                results.append(f"key {a['combo']}")
            else:
                raise PiKvmError(f"action {i}: unknown action {kind!r}")
    return results
