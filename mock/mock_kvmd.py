"""Mock kvmd device implementing the PiKVM API surface pikvm-agent uses.

Serves plain HTTP (no TLS) on 127.0.0.1. Every HID event — HTTP or
WebSocket — is appended to an events JSONL file so tests and UAT can
assert on the exact sequence the harness produced.

Run standalone:  python -m mock.mock_kvmd [--port 8686] [--events events.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
from pathlib import Path

from aiohttp import web, WSMsgType

MOCK_USER = "admin"
MOCK_PASSWD = "admin"
SESSION_COOKIE = "kvmd_session"
SESSION_VALUE = "mock-session-token"

FRAMES_DIR = Path(__file__).parent / "frames"
DEFAULT_FRAME = FRAMES_DIR / "desktop.jpg"


def _make_fallback_frame() -> bytes:
    """A generated 1280x720 test frame used when no canned JPEG exists."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1280, 720), (24, 32, 48))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 690, 1280, 720], fill=(10, 14, 22))  # taskbar
    d.text((20, 698), "MOCK KVMD — taskbar", fill=(220, 220, 220))
    d.text((480, 340), "mock kvmd test frame 1280x720", fill=(240, 240, 240))
    d.rectangle([200, 150, 700, 500], outline=(90, 140, 220), width=3)  # a "window"
    d.text((215, 165), "Terminal — user@mockhost", fill=(160, 200, 160))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class MockKvmd:
    def __init__(self, events_file: Path | None = None, frame_file: Path | None = None):
        self.events_file = events_file
        self.atx_power = True
        self.snapshot_enabled = True  # False simulates Janus-only mode (snapshot 404s)
        self.mouse_online = True
        self.keyboard_online = True
        self.mouse_absolute = True
        self.mouse_output_active = "usb"
        self.mouse_outputs_available = ["usb", "usb_rel"]
        self.snapshot_churn = 0  # >0: return N changing frames (for settle tests)
        frame = frame_file or DEFAULT_FRAME
        self.frame_bytes = frame.read_bytes() if frame.exists() else _make_fallback_frame()
        # host/source resolution reported by /api/streamer; defaults to the frame
        # size so tests don't normalize unless they set a mismatch on purpose
        from PIL import Image
        with Image.open(io.BytesIO(self.frame_bytes)) as _im:
            self.source_resolution = list(_im.size)

    # -- helpers -------------------------------------------------------------

    def record(self, source: str, kind: str, payload: dict) -> None:
        entry = {"ts": time.time(), "source": source, "kind": kind, **payload}
        if self.events_file:
            with self.events_file.open("a") as f:
                f.write(json.dumps(entry) + "\n")

    def _churn_frame(self, n: int) -> bytes:
        """A frame that visibly differs from its neighbours (a block whose
        position depends on n) — makes consecutive frames register as motion."""
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(self.frame_bytes)).convert("RGB")
        d = ImageDraw.Draw(img)
        x = 100 + (n % 5) * 300
        d.rectangle([x, 300, x + 250, 550], fill=(255, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def _authed(self, request: web.Request) -> bool:
        if (
            request.headers.get("X-KVMD-User") == MOCK_USER
            and request.headers.get("X-KVMD-Passwd") == MOCK_PASSWD
        ):
            return True
        return request.cookies.get(SESSION_COOKIE) == SESSION_VALUE

    def _require_auth(self, request: web.Request) -> web.Response | None:
        if not self._authed(request):
            return web.json_response({"ok": False, "result": {"error": "unauthorized"}}, status=401)
        return None

    # -- routes ---------------------------------------------------------------

    async def login(self, request: web.Request) -> web.Response:
        data = await request.post()
        if data.get("user") == MOCK_USER and data.get("passwd") == MOCK_PASSWD:
            resp = web.json_response({"ok": True, "result": {}})
            resp.set_cookie(SESSION_COOKIE, SESSION_VALUE)
            return resp
        return web.json_response({"ok": False, "result": {}}, status=403)

    async def auth_check(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        return web.json_response({"ok": True, "result": {}})

    async def info(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        return web.json_response({
            "ok": True,
            "result": {
                "meta": {"server": {"host": "mock-pikvm"}, "kvm": {}},
                "hw": {"platform": {"base": "Mock v0", "model": "mock"}},
                "system": {"kvmd": {"version": "mock-3.x"}},
            },
        })

    async def hid(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        return web.json_response({
            "ok": True,
            "result": {
                "enabled": True, "busy": False,
                "keyboard": {"online": self.keyboard_online, "leds": {}},
                "mouse": {"online": self.mouse_online, "absolute": self.mouse_absolute,
                          "outputs": {"active": self.mouse_output_active,
                                      "available": self.mouse_outputs_available}},
            },
        })

    async def streamer(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        w, h = self.source_resolution
        return web.json_response({"ok": True, "result": {
            "streamer": {"source": {"online": True, "resolution": {"width": w, "height": h}}}}})

    async def set_params(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        out = request.query.get("mouse_output")
        if out:
            if out not in self.mouse_outputs_available:
                return web.json_response(
                    {"ok": False, "result": {"error": f"unknown mouse output {out!r}"}},
                    status=400)
            self.mouse_output_active = out
            self.mouse_absolute = not out.endswith("_rel")
            self.record("http", "set_params", {"mouse_output": out})
        return web.json_response({"ok": True, "result": {}})

    async def atx(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        return web.json_response({
            "ok": True,
            "result": {"enabled": True, "busy": False,
                       "leds": {"power": self.atx_power, "hdd": False}},
        })

    async def atx_click(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        button = request.query.get("button", "")
        if button not in ("power", "power_long", "reset"):
            return web.json_response({"ok": False, "result": {}}, status=400)
        self.record("http", "atx_click", {"button": button})
        if button in ("power", "power_long"):
            self.atx_power = not self.atx_power
        return web.json_response({"ok": True, "result": {}})

    async def hid_print(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        text = (await request.read()).decode("utf-8")
        self.record("http", "hid_print", {"text": text})
        return web.json_response({"ok": True, "result": {}})

    async def send_key(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        body = await request.json()
        self.record("http", "key", {"key": body.get("key"), "state": body.get("state")})
        return web.json_response({"ok": True, "result": {}})

    async def snapshot(self, request: web.Request) -> web.Response:
        if err := self._require_auth(request):
            return err
        if not self.snapshot_enabled:
            return web.json_response({"ok": False, "result": {}}, status=404)
        if self.snapshot_churn > 0 and request.query.get("ocr") != "1":
            self.snapshot_churn -= 1
            return web.Response(body=self._churn_frame(self.snapshot_churn),
                                content_type="image/jpeg")
        if request.query.get("ocr") == "1":
            return web.Response(text="mock kvmd test frame 1280x720\nTerminal - user@mockhost",
                                content_type="text/plain")
        return web.Response(body=self.frame_bytes, content_type="image/jpeg")

    async def stream(self, request: web.Request) -> web.StreamResponse:
        """Minimal MJPEG multipart stream (repeats the same frame)."""
        if err := self._require_auth(request):
            return err
        boundary = "mockframe"
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": f"multipart/x-mixed-replace; boundary={boundary}"},
        )
        await resp.prepare(request)
        try:
            for _ in range(30):
                part = (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(self.frame_bytes)}\r\n\r\n"
                ).encode() + self.frame_bytes + b"\r\n"
                await resp.write(part)
                await asyncio.sleep(0.05)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._authed(request):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"event_type": "ping", "event": {}})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            event_type = data.get("event_type")
            if event_type == "pong":
                continue
            self.record("ws", event_type or "unknown", {"event": data.get("event", {})})
        return ws

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_get("/api/auth/check", self.auth_check)
        app.router.add_get("/api/info", self.info)
        app.router.add_get("/api/hid", self.hid)
        app.router.add_get("/api/atx", self.atx)
        app.router.add_post("/api/atx/click", self.atx_click)
        app.router.add_post("/api/hid/print", self.hid_print)
        app.router.add_post("/api/hid/set_params", self.set_params)
        app.router.add_get("/api/streamer", self.streamer)
        app.router.add_post("/api/hid/events/send_key", self.send_key)
        app.router.add_get("/api/streamer/snapshot", self.snapshot)
        app.router.add_get("/stream", self.stream)
        app.router.add_get("/api/ws", self.ws)
        return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock PiKVM kvmd device")
    parser.add_argument("--port", type=int, default=8686)
    parser.add_argument("--events", type=Path, default=Path("mock-events.jsonl"),
                        help="JSONL file where HID/ATX events are recorded")
    parser.add_argument("--frame", type=Path, default=None,
                        help="JPEG to serve as the screen frame")
    args = parser.parse_args()
    mock = MockKvmd(events_file=args.events, frame_file=args.frame)
    print(f"mock kvmd on http://127.0.0.1:{args.port} (user/pass {MOCK_USER}/{MOCK_PASSWD}), "
          f"events -> {args.events}")
    web.run_app(mock.build_app(), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
