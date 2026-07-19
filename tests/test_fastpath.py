"""Fast-path: persistent session socket, batched one-socket sequences, and the
image payload budget that keeps a frame under the MCP client's size cap."""

import io
import time

from PIL import Image

from pikvm_agent import hid, screen
from pikvm_agent.client import PiKvmClient
from pikvm_agent.config import load_config


def _client() -> PiKvmClient:
    c = PiKvmClient(load_config(mock=True))
    c.ensure_auth()
    return c


def _wait_events(mock_device, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(mock_device.events()) >= n:
            return mock_device.events()
        time.sleep(0.05)
    return mock_device.events()


# -- payload budget -----------------------------------------------------------

def _noisy_jpeg(w=1920, h=1080) -> bytes:
    """A high-entropy frame that won't compress down trivially."""
    import os
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


def test_fit_jpeg_shrinks_oversized_frame():
    big = _noisy_jpeg()
    assert len(big) > 45_000
    fitted = screen.fit_jpeg(big, max_bytes=45_000)
    assert len(fitted) <= 45_000
    Image.open(io.BytesIO(fitted)).verify()  # still a valid JPEG


def test_fit_jpeg_leaves_small_frames_untouched():
    small = _noisy_jpeg(320, 180)
    # a small native-res crop is already under budget -> returned as-is
    if len(small) <= 45_000:
        assert screen.fit_jpeg(small, max_bytes=45_000) is small


# -- persistent session socket ------------------------------------------------

def test_session_ws_is_reused_and_resettable(mock_device):
    with _client() as c:
        first = c.session_ws()
        assert c.session_ws() is first          # same socket reused
        c.reset_session_ws()
        assert c.session_ws() is not first      # reopened after reset


def test_borrowed_socket_actions_land(mock_device):
    with _client() as c:
        sock = c.session_ws()
        hid.mouse_move(c, 100, 100, ws=sock)
        hid.click(c, "left", 200, 200, ws=sock)
    kinds = [e["kind"] for e in _wait_events(mock_device, 4)]
    assert "mouse_move" in kinds and "mouse_button" in kinds


# -- one socket per batch -----------------------------------------------------

def test_run_sequence_batches_four_clicks(mock_device):
    tiles = [{"action": "click", "x": x, "y": y}
             for x, y in [(220, 390), (350, 520), (480, 520), (220, 650)]]
    with _client() as c:
        results = hid.run_sequence(c, tiles, ws=c.session_ws())
    assert results == ["left click"] * 4
    # 4 clicks => 4 press+release pairs = 8 mouse_button events (+ 4 moves)
    events = _wait_events(mock_device, 12)
    assert sum(1 for e in events if e["kind"] == "mouse_button") == 8
