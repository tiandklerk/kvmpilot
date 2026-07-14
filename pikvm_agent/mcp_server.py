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


@mcp.tool()
def screenshot() -> ImageContent:
    """Capture the target's current screen as a JPEG image. Use this to see
    what is displayed and to verify the result of any input action."""
    with _client() as c:
        snap = screen.capture(c)
    return ImageContent(
        type="image",
        data=base64.b64encode(snap.jpeg).decode("ascii"),
        mimeType="image/jpeg",
    )


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
def type_text(text: str, confirm_token: str | None = None) -> str | list:
    """Type text on the target. Dangerous command text is blocked and returns
    a confirmation token the human must approve (pass it back as confirm_token)."""
    try:
        with _client() as c:
            path = hid.type_text(c, text, confirm_token=confirm_token)
    except guard.GuardError as e:
        return [_guard_message(e)]
    return f"typed {len(text)} chars via {path}"


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
