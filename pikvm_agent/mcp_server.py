"""MCP server exposing the PiKVM harness as tools over stdio.

Any MCP-capable agent (Claude Code/Desktop, OpenClaw, Hermes, ...) can
drive the target: capture the screen (returned as an inline image),
move/click/type, check status, and take guarded power actions. The same
guardrails as the CLI apply — dangerous actions return a token that a
human must approve, surfaced back through `confirm_action`.

Run:  pikvm mcp   (or:  python -m pikvm_agent.mcp_server)
"""

from __future__ import annotations

import base64

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from . import atx as atx_mod
from . import guard, hid, screen
from .client import PiKvmClient, PiKvmError
from .config import load_config

mcp = FastMCP("pikvm")


def _client() -> PiKvmClient:
    client = PiKvmClient(load_config())
    client.ensure_auth()
    return client


def _guard_message(e: guard.GuardError) -> TextContent:
    return TextContent(
        type="text",
        text=(
            f"BLOCKED: {e.reason}. This action is dangerous and requires human "
            f"approval. Show the user this and, only if they approve, call "
            f"confirm_action with token \"{e.token}\". Do not confirm on your own."
        ),
    )


@mcp.tool()
def system_status() -> str:
    """Device reachability, ATX power state, and keyboard/mouse HID online status."""
    with _client() as c:
        info = c.info()
        a = c.atx_state()
        try:
            h = c._get_json("/api/hid").get("result", {})
        except PiKvmError:
            h = {}
    host = info.get("meta", {}).get("server", {}).get("host", "?")
    leds = a.get("leds") or {}
    mouse = h.get("mouse", {})
    lines = [
        f"device: {host} (kvmd {info.get('system', {}).get('kvmd', {}).get('version', '?')})",
        f"power LED: {'on' if leds.get('power') else 'off'}",
        f"keyboard: {'online' if h.get('keyboard', {}).get('online') else 'OFFLINE'}",
        f"mouse: {'online' if mouse.get('online') else 'OFFLINE'} "
        f"({'absolute' if mouse.get('absolute') else 'relative'})",
    ]
    if mouse.get("online") is False:
        lines.append("warning: host has not enumerated the USB mouse; moves/clicks won't land.")
    return "\n".join(lines)


def _image(snap) -> ImageContent:
    return ImageContent(
        type="image",
        data=base64.b64encode(snap.jpeg).decode("ascii"),
        mimeType="image/jpeg",
    )


@mcp.tool()
def screenshot(crop: list[int] | None = None, settle: bool = False) -> ImageContent:
    """Capture the target's current screen as a JPEG. Use it to see the screen
    and to verify the result of an action.

    crop=[x,y,w,h] returns just that region at native resolution — far cheaper
    in tokens than the full frame with no loss of legibility, and coordinates
    still refer to the full screen (add the crop origin back when clicking).
    settle=True waits until the screen stops changing before capturing, instead
    of you guessing a delay after an action."""
    box = tuple(crop) if crop else None
    with _client() as c:
        if settle:
            snap, _ = screen.wait_until_stable(c, crop=box)
        else:
            snap = screen.capture(c, crop=box)
    return _image(snap)


@mcp.tool()
def wait_for_stable(timeout: float = 8.0, crop: list[int] | None = None) -> ImageContent:
    """Wait until the screen stops changing (frame-diff), then return a
    screenshot. Use after an action instead of a blind delay."""
    box = tuple(crop) if crop else None
    with _client() as c:
        snap, _ = screen.wait_until_stable(c, timeout=timeout, crop=box)
    return _image(snap)


@mcp.tool()
def screen_ocr() -> str:
    """Server-side OCR (Tesseract) of the current screen; text only."""
    with _client() as c:
        return screen.capture_ocr(c)


@mcp.tool()
def mouse_move(x: float, y: float) -> str:
    """Move the mouse to pixel coordinates (x, y) on the target screen."""
    with _client() as c:
        hid.mouse_move(c, x, y)
    return f"moved to ({x}, {y})"


@mcp.tool()
def mouse_click(button: str = "left", x: float | None = None, y: float | None = None,
                double: bool = False) -> str:
    """Click a mouse button ("left"/"right"/"middle"), optionally moving to
    (x, y) first. Set double=True for a double-click."""
    with _client() as c:
        hid.click(c, button, x, y, double=double)
    return f"{'double-' if double else ''}{button} click"


@mcp.tool()
def mouse_drag(x1: float, y1: float, x2: float, y2: float) -> str:
    """Left-drag from (x1, y1) to (x2, y2) in target pixels."""
    with _client() as c:
        hid.drag(c, x1, y1, x2, y2)
    return f"dragged ({x1},{y1}) -> ({x2},{y2})"


@mcp.tool()
def wheel(dy: int) -> str:
    """Scroll the mouse wheel; positive dy scrolls up, negative down."""
    with _client() as c:
        hid.wheel(c, dy)
    return f"wheel {dy}"


@mcp.tool()
def type_text(text: str, submit: bool = False, confirm_token: str | None = None) -> str | list:
    """Type text on the target. submit=True presses Enter right after (type-and-
    run in one call). Dangerous command text is blocked and returns a
    confirmation token the human must approve (pass it back as confirm_token)."""
    try:
        with _client() as c:
            path = hid.type_text(c, text, confirm_token=confirm_token, submit=submit)
    except guard.GuardError as e:
        return [_guard_message(e)]
    return f"typed {len(text)} chars via {path}" + (" + Enter" if submit else "")


@mcp.tool()
def run_actions(actions: list[dict], confirm_token: str | None = None) -> str | list:
    """Run a sequence of input steps in one call — one round-trip for a trusted
    sub-sequence instead of one per step. Each item is a dict with an "action":
    move{x,y} | click{button?,x?,y?,double?} | drag{x1,y1,x2,y2} | wheel{dy} |
    type{text,submit?} | key{combo}. A blocked step returns a token and stops
    the sequence there. Take a screenshot afterwards to verify."""
    try:
        with _client() as c:
            results = hid.run_sequence(c, actions, confirm_token=confirm_token)
    except guard.GuardError as e:
        return [_guard_message(e)]
    return "ran: " + "; ".join(results)


@mcp.tool()
def press_keys(combo: str, confirm_token: str | None = None) -> str | list:
    """Press a key combo, e.g. "ctrl+alt+t", "enter", "meta". Power chords and
    an Enter that would run a dangerous typed command are gated by a token."""
    try:
        with _client() as c:
            keys = hid.press_combo(c, combo, confirm_token=confirm_token)
    except guard.GuardError as e:
        return [_guard_message(e)]
    return f"pressed {'+'.join(keys)}"


@mcp.tool()
def atx_action(button: str, confirm_token: str | None = None) -> str | list:
    """ATX power control: "power", "power_long" (hard off), or "reset".
    Always guarded — returns a token the human must approve."""
    try:
        with _client() as c:
            atx_mod.click(c, button, confirm_token=confirm_token)
    except guard.GuardError as e:
        return [_guard_message(e)]
    return f"atx {button} sent"


@mcp.tool()
def confirm_action(token: str) -> str:
    """Execute the pending guarded action using a token the human approved."""
    pending = guard.peek_pending()
    if pending is None:
        return "no pending action to confirm (it may have expired)"
    kind, _, payload = pending["action"].partition(":")
    with _client() as c:
        if kind == "atx":
            atx_mod.click(c, payload, confirm_token=token)
        elif kind == "type":
            hid.type_text(c, payload, confirm_token=token)
        elif kind == "key":
            hid.press_combo(c, payload, confirm_token=token)
        else:
            return f"unknown pending action kind {kind!r}"
    return f"confirmed and executed: {pending['reason']}"


def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the MCP server. transport="stdio" (default) for a local client that
    spawns this process; "streamable-http" to host it as a network service on
    host:port (e.g. on a LAN box behind Tailscale — never expose it publicly)."""
    if transport == "streamable-http":
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
