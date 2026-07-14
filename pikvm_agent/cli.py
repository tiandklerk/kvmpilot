"""`pikvm` command-line interface.

Exit codes: 0 ok, 1 error, 2 usage, 3 action blocked by guardrail
(a confirm token was issued — see `pikvm confirm`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import guard
from .client import PiKvmClient, PiKvmError
from .config import ConfigError, load_config

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 3


def cmd_status(client: PiKvmClient, args: argparse.Namespace) -> int:
    client.ensure_auth()
    info = client.info()
    atx = client.atx_state()
    try:
        _, content_type = client.get_bytes("/api/streamer/snapshot")
        snapshot = "available" if content_type.startswith("image/") else f"unexpected ({content_type})"
    except PiKvmError as e:
        # kvmd suspends the streamer with no KVM client attached; `screenshot`
        # wakes it via /api/ws, so this is not a hard failure.
        snapshot = "suspended (resumes on capture)" if "503" in str(e) else f"unavailable ({e})"

    try:
        hid = client._get_json("/api/hid").get("result", {})
    except PiKvmError:
        hid = {}
    kbd_online = hid.get("keyboard", {}).get("online")
    mouse = hid.get("mouse", {})
    mouse_online = mouse.get("online")
    mouse_mode = "absolute" if mouse.get("absolute") else "relative"

    platform = info.get("hw", {}).get("platform", {})
    meta_host = info.get("meta", {}).get("server", {}).get("host", "?")
    kvmd_version = info.get("system", {}).get("kvmd", {}).get("version", "?")
    leds = atx.get("leds") or {}
    print(f"device:       {meta_host} ({platform.get('base', '?')}, kvmd {kvmd_version})")
    print(f"url:          {client.config.base_url}")
    print(f"atx:          enabled={atx.get('enabled')} power_led={'on' if leds.get('power') else 'off'}")
    print(f"snapshot:     {snapshot}")
    print(f"keyboard:     {'online' if kbd_online else 'OFFLINE'}")
    print(f"mouse:        {'online' if mouse_online else 'OFFLINE'} ({mouse_mode})")
    if mouse_online is False:
        print("  warning:    the host has not enumerated the USB mouse gadget; "
              "mouse moves/clicks will not reach it until it comes online "
              "(re-plug the OTG, reboot the host, or try the usb_rel output).")
    if args.json:
        print(json.dumps({"info": info, "atx": atx, "hid": hid, "snapshot": snapshot}, indent=2))
    return EXIT_OK


def cmd_info(client: PiKvmClient, args: argparse.Namespace) -> int:
    client.ensure_auth()
    print(json.dumps(client.info(), indent=2))
    return EXIT_OK


def cmd_screenshot(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import screen

    client.ensure_auth()
    if args.ocr:
        print(screen.capture_ocr(client))
        return EXIT_OK
    snap = screen.capture(client)
    path = snap.save(args.output)
    print(f"saved:      {path}")
    print(f"resolution: {snap.width}x{snap.height}")
    print(f"source:     {snap.source}")
    return EXIT_OK


def cmd_move(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import hid

    client.ensure_auth()
    hid.mouse_move(client, args.x, args.y)
    print(f"moved to ({args.x}, {args.y})")
    return EXIT_OK


def cmd_click(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import hid

    client.ensure_auth()
    hid.click(client, args.button, args.x, args.y, double=args.double)
    where = f" at ({args.x}, {args.y})" if args.x is not None else ""
    print(f"{'double-' if args.double else ''}{args.button} click{where}")
    return EXIT_OK


def cmd_drag(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import hid

    client.ensure_auth()
    hid.drag(client, args.x1, args.y1, args.x2, args.y2)
    print(f"dragged ({args.x1}, {args.y1}) -> ({args.x2}, {args.y2})")
    return EXIT_OK


def cmd_wheel(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import hid

    client.ensure_auth()
    hid.wheel(client, args.dy)
    print(f"wheel {args.dy}")
    return EXIT_OK


def cmd_type(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import hid

    client.ensure_auth()
    path = hid.type_text(client, args.text, confirm_token=args.confirm)
    print(f"typed {len(args.text)} chars via {path}")
    return EXIT_OK


def cmd_key(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import hid

    client.ensure_auth()
    keys = hid.press_combo(client, args.combo, confirm_token=args.confirm)
    print(f"pressed {'+'.join(keys)}")
    return EXIT_OK


def cmd_atx(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import atx

    client.ensure_auth()
    atx.click(client, args.button, confirm_token=args.confirm)
    print(f"atx {args.button} sent")
    return EXIT_OK


def cmd_confirm(client: PiKvmClient, args: argparse.Namespace) -> int:
    """Resolve a pending guarded action by re-dispatching it with the token."""
    pending = guard.peek_pending()
    if pending is None:
        print("no pending action to confirm (it may have expired)", file=sys.stderr)
        return EXIT_ERROR
    action = pending["action"]
    kind, _, payload = action.partition(":")
    client.ensure_auth()
    from . import atx, hid

    if kind == "atx":
        atx.click(client, payload, confirm_token=args.token)
    elif kind == "type":
        hid.type_text(client, payload, confirm_token=args.token)
    elif kind == "key":
        hid.press_combo(client, payload, confirm_token=args.token)
    else:
        print(f"unknown pending action kind {kind!r}", file=sys.stderr)
        return EXIT_ERROR
    print(f"confirmed and executed: {pending['reason']}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pikvm",
        description="Operate a computer through a PiKVM: screen capture + HID input with guardrails.",
    )
    parser.add_argument("--mock", action="store_true",
                        help="target the local mock kvmd (see mock/mock_kvmd.py)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="device reachability, ATX power state, snapshot availability")
    p.add_argument("--json", action="store_true", help="also dump raw JSON")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("info", help="raw /api/info JSON")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("screenshot", help="capture the target's screen to a JPEG")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="output file (default: ~/.local/state/pikvm-agent/screenshots/screen-<ts>.jpg)")
    p.add_argument("--ocr", action="store_true",
                   help="print server-side OCR text of the frame instead of saving an image")
    p.set_defaults(func=cmd_screenshot)

    p = sub.add_parser("move", help="move mouse to pixel coordinates")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("click", help="click a mouse button, optionally at pixel coordinates")
    p.add_argument("button", nargs="?", default="left", choices=["left", "right", "middle"])
    p.add_argument("x", nargs="?", type=float, default=None)
    p.add_argument("y", nargs="?", type=float, default=None)
    p.add_argument("--double", action="store_true")
    p.set_defaults(func=cmd_click)

    p = sub.add_parser("drag", help="left-drag between two pixel coordinates")
    p.add_argument("x1", type=float)
    p.add_argument("y1", type=float)
    p.add_argument("x2", type=float)
    p.add_argument("y2", type=float)
    p.set_defaults(func=cmd_drag)

    p = sub.add_parser("wheel", help="scroll: positive dy scrolls up, negative down")
    p.add_argument("dy", type=int)
    p.set_defaults(func=cmd_wheel)

    p = sub.add_parser("type", help="type text on the target")
    p.add_argument("text")
    p.add_argument("--confirm", metavar="TOKEN", default=None,
                   help="token to authorize a previously blocked dangerous command")
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("key", help='press a key combo, e.g. "ctrl+alt+t", "meta", "enter"')
    p.add_argument("combo")
    p.add_argument("--confirm", metavar="TOKEN", default=None,
                   help="token to authorize a blocked power chord / dangerous Enter")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("atx", help="ATX power control (always guarded)")
    p.add_argument("button", choices=["power", "power_long", "reset"])
    p.add_argument("--confirm", metavar="TOKEN", default=None,
                   help="token to authorize this power action")
    p.set_defaults(func=cmd_atx)

    p = sub.add_parser("confirm", help="execute the pending guarded action with its token")
    p.add_argument("token")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("mcp", help="run the MCP server (stdio) for MCP-capable agents")
    p.set_defaults(func=None)  # handled specially in main() before opening a client

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp":
        if args.mock:
            import os

            os.environ["PIKVM_MOCK"] = "1"  # propagate to the server's load_config()
        from .mcp_server import main as mcp_main

        mcp_main()
        return EXIT_OK
    try:
        config = load_config(mock=args.mock)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with PiKvmClient(config) as client:
            return args.func(client, args)
    except guard.GuardError as e:
        print(f"\n{e}", file=sys.stderr)
        print(f"\nTo proceed, a human must approve this. Confirm with:\n"
              f"    pikvm{' --mock' if args.mock else ''} confirm {e.token}\n"
              f"(token valid for {guard.TOKEN_TTL // 60} minutes)", file=sys.stderr)
        return EXIT_BLOCKED
    except PiKvmError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except (ConnectionError, OSError) as e:
        print(f"error: cannot reach {config.base_url}: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
