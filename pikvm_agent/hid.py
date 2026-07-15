"""HID input: mouse (absolute, over the kvmd WebSocket) and keyboard.

Coordinates are given in target-screen pixels and mapped to kvmd's
signed absolute range [-32768, 32767] (same math as the PiKVM web UI):
    kvm = int(px / size * 65535 - 32768)
The target resolution comes from the last screenshot (cached in the
state dir) so mouse commands don't need a capture each time.
"""

from __future__ import annotations

import json
import time

from . import guard, keymap
from .client import PiKvmClient, PiKvmError

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

def mouse_move(client: PiKvmClient, x: float, y: float) -> None:
    w, h = get_resolution(client)
    kx, ky = pixel_to_kvm(x, y, w, h)
    with client.open_ws() as ws:
        ws.mouse_move(kx, ky)
        time.sleep(FLUSH_DELAY)


def click(
    client: PiKvmClient,
    button: str = "left",
    x: float | None = None,
    y: float | None = None,
    double: bool = False,
) -> None:
    if button not in ("left", "right", "middle"):
        raise PiKvmError(f"unknown mouse button {button!r}")
    w, h = get_resolution(client)
    with client.open_ws() as ws:
        if x is not None and y is not None:
            ws.mouse_move(*pixel_to_kvm(x, y, w, h))
            time.sleep(KEY_DELAY)
        for _ in range(2 if double else 1):
            ws.mouse_button(button, True)
            time.sleep(KEY_DELAY)
            ws.mouse_button(button, False)
            time.sleep(KEY_DELAY)
        time.sleep(FLUSH_DELAY)


def drag(client: PiKvmClient, x1: float, y1: float, x2: float, y2: float, steps: int = 12) -> None:
    w, h = get_resolution(client)
    with client.open_ws() as ws:
        ws.mouse_move(*pixel_to_kvm(x1, y1, w, h))
        time.sleep(KEY_DELAY)
        ws.mouse_button("left", True)
        time.sleep(KEY_DELAY)
        for i in range(1, steps + 1):
            t = i / steps
            ws.mouse_move(*pixel_to_kvm(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, w, h))
            time.sleep(KEY_DELAY)
        ws.mouse_button("left", False)
        time.sleep(FLUSH_DELAY)


def wheel(client: PiKvmClient, dy: int, dx: int = 0) -> None:
    with client.open_ws() as ws:
        ws.mouse_wheel(dx, dy)
        time.sleep(FLUSH_DELAY)


# -- keyboard -----------------------------------------------------------------

def type_text(client: PiKvmClient, text: str, force_ws: bool = False,
              confirm_token: str | None = None, submit: bool = False) -> str:
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
        _type_text_ws(client, text)

    if submit:
        # `text` was already gated above; clear the buffer so the Enter isn't
        # re-flagged by the type-then-Enter evasion check.
        guard.clear_typed_buffer()
        with client.open_ws() as ws:
            ws.key("Enter", True)
            time.sleep(KEY_DELAY)
            ws.key("Enter", False)
            time.sleep(FLUSH_DELAY)
        guard.audit("key:Enter", "executed", "submit after type")
    return path


def _type_text_ws(client: PiKvmClient, text: str) -> None:
    with client.open_ws() as ws:
        for c in text:
            mapped = keymap.from_char(c)
            if mapped is None:
                raise PiKvmError(f"cannot type character {c!r} via key events")
            if mapped.shift:
                ws.key("ShiftLeft", True)
                time.sleep(KEY_DELAY)
            ws.key(mapped.key, True)
            time.sleep(KEY_DELAY)
            ws.key(mapped.key, False)
            time.sleep(KEY_DELAY)
            if mapped.shift:
                ws.key("ShiftLeft", False)
                time.sleep(KEY_DELAY)
        time.sleep(FLUSH_DELAY)


def press_combo(client: PiKvmClient, combo: str, confirm_token: str | None = None) -> list[str]:
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

    with client.open_ws() as ws:
        for k in keys:
            ws.key(k, True)
            time.sleep(KEY_DELAY)
        for k in reversed(keys):
            ws.key(k, False)
            time.sleep(KEY_DELAY)
        time.sleep(FLUSH_DELAY)
    return keys


# -- compound actions ---------------------------------------------------------

def run_sequence(client: PiKvmClient, actions: list[dict],
                 confirm_token: str | None = None) -> list[str]:
    """Run a list of input steps in order, so an agent spends one round-trip on
    a trusted sub-sequence instead of one per step. Each action is a dict with
    an "action" key: move{x,y} | click{button?,x?,y?,double?} | drag{x1,y1,x2,y2}
    | wheel{dy} | type{text,submit?} | key{combo}. Guardrails apply per step; a
    blocked step raises GuardError and stops the sequence there.
    """
    results: list[str] = []
    for i, a in enumerate(actions):
        kind = a.get("action")
        if kind == "move":
            mouse_move(client, a["x"], a["y"])
            results.append(f"move ({a['x']},{a['y']})")
        elif kind == "click":
            click(client, a.get("button", "left"), a.get("x"), a.get("y"),
                  double=a.get("double", False))
            results.append(f"{a.get('button', 'left')} click")
        elif kind == "drag":
            drag(client, a["x1"], a["y1"], a["x2"], a["y2"])
            results.append("drag")
        elif kind == "wheel":
            wheel(client, a["dy"])
            results.append(f"wheel {a['dy']}")
        elif kind == "type":
            type_text(client, a["text"], confirm_token=confirm_token,
                      submit=a.get("submit", False))
            results.append(f"type {len(a['text'])} chars")
        elif kind == "key":
            press_combo(client, a["combo"], confirm_token=confirm_token)
            results.append(f"key {a['combo']}")
        else:
            raise PiKvmError(f"action {i}: unknown action {kind!r}")
    return results
