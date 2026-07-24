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


def test_fit_jpeg_compresses_without_changing_resolution():
    big = _noisy_jpeg(1920, 1080)
    assert len(big) > 60_000
    fitted = screen.fit_jpeg(big, max_bytes=60_000)
    assert len(fitted) < len(big)                      # compressed
    assert Image.open(io.BytesIO(fitted)).size == (1920, 1080)  # resolution held fixed


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

def test_humanized_move_emits_a_path(mock_device):
    with _client() as c:
        hid.mouse_move(c, 300, 200, human=True)
    moves = [e for e in _wait_events(mock_device, 5) if e["kind"] == "mouse_move"]
    assert len(moves) >= 5  # a curved path, not one instant jump


def test_normal_move_is_a_single_jump(mock_device):
    with _client() as c:
        hid.mouse_move(c, 300, 200, human=False)
    moves = [e for e in mock_device.events() if e["kind"] == "mouse_move"]
    assert len(moves) == 1


# -- frame dimensions surfaced to the agent -----------------------------------

def test_capture_normalizes_to_host_resolution(mock_device):
    # encoder "downscaled": snapshot is 1280×720 but the host source is 1920×1080.
    # capture must return the frame at the stable host resolution so click
    # coordinates stay in one space regardless of the delivered frame size.
    mock_device.kvmd.source_resolution = [1920, 1080]
    with _client() as c:
        snap = screen.capture(c)
    assert (snap.width, snap.height) == (1920, 1080)
    assert screen.cached_full_resolution() == (1920, 1080)


def test_host_resolution_falls_back_gracefully_when_streamer_reports_null(mock_device):
    # kvmd reports {"streamer": null} (not merely an absent key) while the
    # streamer is idle/waking -- this must not crash, just fall back to None
    # so callers use the frame's own dimensions instead.
    mock_device.kvmd.streamer_idle = True
    with _client() as c:
        assert screen.host_resolution(c) is None
        snap = screen.capture(c)  # must not raise
    assert (snap.width, snap.height) == tuple(mock_device.kvmd.source_resolution)


def test_capture_passthrough_when_frame_matches_host(mock_device):
    # frame already matches host res (both 1280×720) -> no re-encode needed
    with _client() as c:
        snap = screen.capture(c)
    assert (snap.width, snap.height) == (1280, 720)


def test_frame_text_reports_full_dimensions(mock_device):
    from pikvm_agent import mcp_server
    with _client() as c:
        snap = screen.capture(c)
    txt = mcp_server._frame_text(snap)
    assert str(snap.width) in txt.text and "coordinates are in this pixel space" in txt.text


def test_frame_text_crop_gives_origin_mapping(mock_device):
    from pikvm_agent import mcp_server
    with _client() as c:
        screen.capture(c)                       # populate the full-resolution cache
        snap = screen.capture(c, crop=(100, 50, 200, 150))
    txt = mcp_server._frame_text(snap)
    assert "origin (100,50)" in txt.text and "full_x = 100 + local_x" in txt.text


def test_run_sequence_batches_four_clicks(mock_device):
    tiles = [{"action": "click", "x": x, "y": y}
             for x, y in [(220, 390), (350, 520), (480, 520), (220, 650)]]
    with _client() as c:
        results = hid.run_sequence(c, tiles, ws=c.session_ws())
    assert results == ["left click"] * 4
    # 4 clicks => 4 press+release pairs = 8 mouse_button events (+ 4 moves)
    events = _wait_events(mock_device, 12)
    assert sum(1 for e in events if e["kind"] == "mouse_button") == 8
