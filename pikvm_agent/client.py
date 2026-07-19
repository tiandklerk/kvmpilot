"""HTTP + WebSocket client for the PiKVM kvmd API.

Auth is stateless X-KVMD-User / X-KVMD-Passwd headers on every request
(HTTP and WebSocket alike), with a cookie-login fallback for kvmd builds
that reject header auth. PiKVMs ship self-signed certs, so TLS
verification is off by default.
"""

from __future__ import annotations

import json
import ssl
from typing import Any

import httpx
from websockets.sync.client import connect as ws_connect

from .config import Config


class PiKvmError(Exception):
    pass


def _insecure_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PiKvmClient:
    def __init__(self, config: Config, timeout: float = 15.0):
        self.config = config
        self._http = httpx.Client(
            base_url=config.base_url,
            headers=config.auth_headers,
            verify=False,
            timeout=timeout,
        )
        self._cookie_auth = False
        self._session_ws: "HidWebSocket | None" = None

    # -- persistent session -------------------------------------------------

    def session_ws(self) -> "HidWebSocket":
        """A long-lived HID WebSocket, reused across actions. Holding it open
        also keeps kvmd's streamer awake, so snapshots skip the wake cost.
        Reopened transparently if it was reset."""
        if self._session_ws is None:
            self._session_ws = self.open_ws()
        return self._session_ws

    def reset_session_ws(self) -> None:
        """Drop the persistent socket (e.g. after a connection error); the next
        session_ws() call reopens it."""
        if self._session_ws is not None:
            try:
                self._session_ws.close()
            except Exception:
                pass
            self._session_ws = None

    # -- auth ---------------------------------------------------------------

    def ensure_auth(self) -> None:
        """Verify header auth works; fall back to cookie login if not."""
        r = self._http.get("/api/auth/check")
        if r.status_code == 200:
            return
        if r.status_code in (401, 403):
            self._login_cookie()
            return
        raise PiKvmError(f"auth check failed: HTTP {r.status_code}")

    def _login_cookie(self) -> None:
        r = self._http.post(
            "/api/auth/login",
            data={"user": self.config.user, "passwd": self.config.password},
        )
        if r.status_code != 200:
            raise PiKvmError(f"login failed: HTTP {r.status_code} {r.text[:200]}")
        self._cookie_auth = True

    # -- HTTP API -----------------------------------------------------------

    def _get_json(self, path: str, **params: Any) -> dict:
        r = self._http.get(path, params=params or None)
        if r.status_code != 200:
            raise PiKvmError(f"GET {path} -> HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def info(self) -> dict:
        return self._get_json("/api/info").get("result", {})

    def atx_state(self) -> dict:
        return self._get_json("/api/atx").get("result", {})

    def atx_click(self, button: str) -> None:
        r = self._http.post("/api/atx/click", params={"button": button})
        if r.status_code != 200:
            raise PiKvmError(f"atx click {button} -> HTTP {r.status_code} {r.text[:200]}")

    def hid_state(self) -> dict:
        return self._get_json("/api/hid").get("result", {})

    def set_mouse_output(self, output: str) -> None:
        """Switch the kvmd mouse gadget (e.g. "usb" = absolute, "usb_rel" =
        relative). The gadget re-enumerates, so it may be briefly offline."""
        r = self._http.post("/api/hid/set_params", params={"mouse_output": output})
        if r.status_code != 200:
            raise PiKvmError(f"set mouse output {output} -> HTTP {r.status_code} {r.text[:200]}")

    def hid_print(self, text: str) -> None:
        r = self._http.post(
            "/api/hid/print",
            content=text.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        if r.status_code != 200:
            raise PiKvmError(f"hid print -> HTTP {r.status_code} {r.text[:200]}")

    def send_key_http(self, key: str, state: bool) -> None:
        r = self._http.post("/api/hid/events/send_key", json={"key": key, "state": state})
        if r.status_code != 200:
            raise PiKvmError(f"send_key {key} -> HTTP {r.status_code} {r.text[:200]}")

    def get_bytes(self, path: str, **params: Any) -> tuple[bytes, str]:
        """GET raw bytes; returns (body, content-type)."""
        r = self._http.get(path, params=params or None)
        if r.status_code != 200:
            raise PiKvmError(f"GET {path} -> HTTP {r.status_code} {r.text[:200]}")
        return r.content, r.headers.get("content-type", "")

    # -- WebSocket (HID events) ----------------------------------------------

    def open_ws(self) -> "HidWebSocket":
        headers = dict(self.config.auth_headers)
        if self._cookie_auth:
            cookies = "; ".join(f"{k}={v}" for k, v in self._http.cookies.items())
            if cookies:
                headers["Cookie"] = cookies
        ssl_ctx = _insecure_ssl_context() if self.config.use_ssl else None
        ws = ws_connect(
            self.config.ws_url,
            additional_headers=headers,
            ssl=ssl_ctx,
            open_timeout=10,
        )
        return HidWebSocket(ws)

    def close(self) -> None:
        self.reset_session_ws()
        self._http.close()

    def __enter__(self) -> "PiKvmClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class HidWebSocket:
    """Thin wrapper over the kvmd /api/ws event socket."""

    def __init__(self, ws):
        self._ws = ws

    def send_event(self, event_type: str, event: dict) -> None:
        self._ws.send(json.dumps({"event_type": event_type, "event": event}))
        self._drain()

    def mouse_move(self, kvm_x: int, kvm_y: int) -> None:
        self.send_event("mouse_move", {"to": {"x": kvm_x, "y": kvm_y}})

    def mouse_button(self, button: str, pressed: bool) -> None:
        self.send_event("mouse_button", {"button": button, "state": pressed})

    def mouse_wheel(self, dx: int, dy: int) -> None:
        self.send_event("mouse_wheel", {"delta": {"x": dx, "y": dy}})

    def key(self, key: str, pressed: bool) -> None:
        self.send_event("key", {"key": key, "state": pressed})

    def _drain(self) -> None:
        """Answer any pending server pings without blocking."""
        while True:
            try:
                msg = self._ws.recv(timeout=0)
            except TimeoutError:
                return
            try:
                if json.loads(msg).get("event_type") == "ping":
                    self._ws.send('{"event_type":"pong"}')
            except (ValueError, AttributeError):
                pass

    def close(self) -> None:
        self._ws.close()

    def __enter__(self) -> "HidWebSocket":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
