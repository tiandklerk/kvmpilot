"""`pikvm` command-line interface.

Exit codes: 0 ok, 1 error, 2 usage, 3 action blocked by guardrail
(a confirm token was issued — see `pikvm confirm`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
        snapshot = f"unavailable ({e})"

    platform = info.get("hw", {}).get("platform", {})
    meta_host = info.get("meta", {}).get("server", {}).get("host", "?")
    kvmd_version = info.get("system", {}).get("kvmd", {}).get("version", "?")
    leds = atx.get("leds") or {}
    print(f"device:       {meta_host} ({platform.get('base', '?')}, kvmd {kvmd_version})")
    print(f"url:          {client.config.base_url}")
    print(f"atx:          enabled={atx.get('enabled')} power_led={'on' if leds.get('power') else 'off'}")
    print(f"snapshot:     {snapshot}")
    if args.json:
        print(json.dumps({"info": info, "atx": atx, "snapshot": snapshot}, indent=2))
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(mock=args.mock)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with PiKvmClient(config) as client:
            return args.func(client, args)
    except PiKvmError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except (ConnectionError, OSError) as e:
        print(f"error: cannot reach {config.base_url}: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
