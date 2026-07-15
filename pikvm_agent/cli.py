"""`pikvm` command-line interface.

Exit codes: 0 ok, 1 error, 2 usage, 3 action blocked by guardrail
(a confirm token was issued — see `pikvm confirm`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
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


def _parse_crop(value: str | None) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    try:
        x, y, w, h = (int(n) for n in value.split(","))
    except ValueError:
        raise ConfigError(f"--crop expects 'x,y,w,h' integers, got {value!r}") from None
    return (x, y, w, h)


def cmd_screenshot(client: PiKvmClient, args: argparse.Namespace) -> int:
    from . import screen

    client.ensure_auth()
    if args.ocr:
        print(screen.capture_ocr(client))
        return EXIT_OK
    crop = _parse_crop(args.crop)
    if args.settle:
        snap, settled = screen.wait_until_stable(client, crop=crop)
        if not settled:
            print("note: screen still changing at timeout", file=sys.stderr)
    else:
        snap = screen.capture(client, crop=crop)
    path = snap.save(args.output)
    print(f"saved:      {path}")
    print(f"resolution: {snap.width}x{snap.height}"
          + (f"  (crop of full screen at {crop[0]},{crop[1]})" if crop else ""))
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
    path = hid.type_text(client, args.text, confirm_token=args.confirm, submit=args.enter)
    print(f"typed {len(args.text)} chars via {path}" + (" + Enter" if args.enter else ""))
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


def _endpoint_from_args(args: argparse.Namespace) -> config_mod.Endpoint:
    """Resolve host/user from --host/--user flags, else the selected profile."""
    if args.host:
        return config_mod.Endpoint(
            host=args.host,
            user=args.user or "admin",
            use_ssl=not args.no_ssl,
            port=args.port,
        )
    ep = config_mod.resolve_endpoint(getattr(args, "profile", None))
    if args.user:
        ep.user = args.user
    return ep


def cmd_profiles(args: argparse.Namespace) -> int:
    """List configured PiKVM profiles."""
    profiles = config_mod.list_profiles()
    if not profiles:
        print("no profiles configured — run `pikvm setup`")
        return EXIT_OK
    default = config_mod.default_profile_name()
    for name, ep in profiles.items():
        has_pw = ep.file_password is not None or config_mod.get_stored_password(ep.host, ep.user)
        mark = "*" if name == default else " "
        scheme = "https" if ep.use_ssl else "http"
        print(f"{mark} {name:<16} {scheme}://{ep.user}@{ep.host}"
              f"{'  [password set]' if has_pw else '  [no password — run pikvm login]'}")
    print("\n(* = default)")
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive onboarding: add a profile, store its password, verify, and
    print the MCP registration command."""
    import getpass

    print("pikvm setup — add a PiKVM profile\n")
    name = args.name or input("profile name [default]: ").strip() or "default"
    host = args.host or input("host or IP: ").strip()
    if not host:
        print("error: a host is required", file=sys.stderr)
        return EXIT_ERROR
    user = args.user or input("user [admin]: ").strip() or "admin"
    ssl_in = ("n" if args.no_ssl else input("use TLS/https? [Y/n]: ").strip().lower())
    use_ssl = ssl_in not in ("n", "no")
    ep = config_mod.Endpoint(host=host, user=user, use_ssl=use_ssl, port=args.port)

    password = args.password or getpass.getpass(f"kvmd password for {user}@{host}: ")
    if not password:
        print("error: a password is required", file=sys.stderr)
        return EXIT_ERROR

    cfg = config_mod.Config(host, user, password, use_ssl, args.port)
    try:
        with PiKvmClient(cfg) as client:
            client.ensure_auth()
    except (PiKvmError, ConnectionError, OSError) as e:
        print(f"error: could not verify credentials against {cfg.base_url}: {e}",
              file=sys.stderr)
        return EXIT_ERROR

    config_mod.save_profile(name, ep, make_default=args.default)
    stored_in = "keyring"
    try:
        config_mod.set_stored_password(host, user, password)
    except ConfigError:
        # no secret store here — fall back to writing the password into the profile
        stored_in = "config file (no keyring daemon available)"
        data = config_mod._read_toml()
        data["profiles"][name]["password"] = password
        import tomli_w
        config_mod.CONFIG_PATH.write_text(tomli_w.dumps(data))
        config_mod.CONFIG_PATH.chmod(0o600)

    print(f"\n✓ profile '{name}' saved and verified (password in {stored_in}).")
    print("register it with an MCP client, e.g. Claude Code:\n")
    print("    claude mcp add pikvm -- pikvm mcp")
    print(f"\nuse it from the CLI with:  pikvm --profile {name} status")
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    """Import a pikonnect-style registry export (many profiles at once)."""
    from pathlib import Path as _Path

    try:
        names = config_mod.import_registry(_Path(args.file))
    except (ConfigError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    print(f"imported {len(names)} profile(s): {', '.join(names)}")
    print("profiles without a bundled password: run `pikvm login --profile <name>`.")
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    """Store a kvmd password in the OS keyring and verify it connects."""
    import getpass

    if not config_mod.keyring_available():
        print("error: the 'keyring' package is not installed, so there is no OS secret "
              "store. Install it (pip install keyring) or use PIKVM_PASSWORD / the TOML "
              "file.", file=sys.stderr)
        return EXIT_ERROR
    try:
        ep = _endpoint_from_args(args)
    except ConfigError as e:
        print(f"error: {e} (or pass --host/--user)", file=sys.stderr)
        return EXIT_ERROR

    password = args.password or getpass.getpass(f"kvmd password for {ep.user}@{ep.host}: ")
    if not password:
        print("error: empty password", file=sys.stderr)
        return EXIT_ERROR

    if not args.no_verify:
        cfg = config_mod.Config(ep.host, ep.user, password, ep.use_ssl, ep.port)
        try:
            with PiKvmClient(cfg) as client:
                client.ensure_auth()
        except (PiKvmError, ConnectionError, OSError) as e:
            print(f"error: credentials did not verify against {cfg.base_url}: {e}\n"
                  "nothing stored. Re-run with --no-verify to store anyway.", file=sys.stderr)
            return EXIT_ERROR

    config_mod.set_stored_password(ep.host, ep.user, password)
    print(f"stored password for {ep.user}@{ep.host} in the OS keyring"
          f"{'' if args.no_verify else ' (verified)'}.")
    if ep.file_password:
        print(f"note: {config_mod.CONFIG_PATH} still has a plaintext `password` line; "
              "remove it so the keyring is used.")
    return EXIT_OK


def cmd_logout(args: argparse.Namespace) -> int:
    """Remove a stored password from the OS keyring."""
    try:
        ep = _endpoint_from_args(args)
    except ConfigError as e:
        print(f"error: {e} (or pass --host/--user)", file=sys.stderr)
        return EXIT_ERROR
    if config_mod.delete_stored_password(ep.host, ep.user):
        print(f"removed keyring entry for {ep.user}@{ep.host}.")
    else:
        print(f"no keyring entry for {ep.user}@{ep.host}.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pikvm",
        description="Operate a computer through a PiKVM: screen capture + HID input with guardrails.",
    )
    parser.add_argument("--mock", action="store_true",
                        help="target the local mock kvmd (see mock/mock_kvmd.py)")
    parser.add_argument("--profile", default=None,
                        help="which configured PiKVM profile to use (see `pikvm profiles`)")
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
    p.add_argument("--crop", metavar="X,Y,W,H", default=None,
                   help="capture only this region at native resolution (full-screen pixels)")
    p.add_argument("--settle", action="store_true",
                   help="wait until the screen stops changing before capturing")
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
    p.add_argument("--enter", action="store_true",
                   help="press Enter right after typing (type-and-run in one call)")
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

    p = sub.add_parser("mcp", help="run the MCP server for MCP-capable agents")
    p.add_argument("--http", action="store_true",
                   help="serve over streamable-HTTP instead of stdio (for hosting on a LAN box)")
    p.add_argument("--bind", default="127.0.0.1",
                   help="HTTP bind address (default 127.0.0.1; use 0.0.0.0 to expose on the LAN)")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    p.set_defaults(func=None)  # handled specially in main() before opening a client

    p = sub.add_parser("login", help="store a kvmd password in the OS keyring (recommended)")
    p.add_argument("--host", default=None, help="override host (else uses env/TOML)")
    p.add_argument("--user", default=None, help="override user (default admin)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-ssl", action="store_true", help="target http instead of https")
    p.add_argument("--password", default=None,
                   help="password (omit to be prompted without echo — preferred)")
    p.add_argument("--no-verify", action="store_true",
                   help="store without first checking the password connects")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="remove a stored password from the OS keyring")
    p.add_argument("--host", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-ssl", action="store_true")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("setup", help="interactive onboarding: add a PiKVM profile")
    p.add_argument("--name", default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-ssl", action="store_true")
    p.add_argument("--password", default=None, help="skip the prompt (not recommended)")
    p.add_argument("--default", action="store_true", help="make this the default profile")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("profiles", help="list configured PiKVM profiles")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("import", help="import a pikonnect registry export (JSON)")
    p.add_argument("file", help="path to the exported registry .json")
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp":
        if args.mock:
            import os

            os.environ["PIKVM_MOCK"] = "1"  # propagate to the server's load_config()
        from .mcp_server import main as mcp_main

        if args.http:
            mcp_main(transport="streamable-http", host=args.bind, port=args.port)
        else:
            mcp_main()
        return EXIT_OK
    if args.command in ("login", "logout", "setup", "profiles", "import"):
        try:
            return args.func(args)
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ERROR
    try:
        config = load_config(mock=args.mock, profile=args.profile)
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
