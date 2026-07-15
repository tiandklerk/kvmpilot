"""Elegance enhancements: cropping, frame-diff settle, compound actions."""

import time

import pytest

from pikvm_agent import hid, screen
from pikvm_agent.cli import main
from pikvm_agent.client import PiKvmClient
from pikvm_agent.config import load_config


def _wait_events(mock_device, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(mock_device.events()) >= n:
            return mock_device.events()
        time.sleep(0.05)
    return mock_device.events()


# -- cropping -----------------------------------------------------------------

def test_crop_returns_region_at_native_res(mock_device):
    with PiKvmClient(load_config()) as client:
        full = screen.capture(client)
        snap = screen.capture(client, crop=(100, 200, 400, 300))
    assert (full.width, full.height) == (1280, 720)      # mock frame
    assert (snap.width, snap.height) == (400, 300)       # cropped region
    assert snap.crop == (100, 200, 400, 300)
    # the resolution cache still reflects the FULL screen, so click mapping is unaffected
    import json
    cached = json.loads(screen.RESOLUTION_CACHE.read_text())
    assert (cached["width"], cached["height"]) == (1280, 720)


def test_cli_screenshot_crop(mock_device, tmp_path, capsys):
    out = tmp_path / "c.jpg"
    assert main(["--mock", "screenshot", "--crop", "0,0,200,100", "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "200x100" in printed
    from PIL import Image
    assert Image.open(out).size == (200, 100)


# -- frame-diff settle --------------------------------------------------------

def test_frame_diff_zero_for_identical(mock_device):
    with PiKvmClient(load_config()) as client:
        a = screen.capture(client).jpeg
        b = screen.capture(client).jpeg   # mock serves the same frame
    assert screen._frame_diff(a, b) < 1.0


def test_wait_until_stable_detects_motion_then_settles(mock_device):
    mock_device.kvmd.snapshot_churn = 4   # first 4 frames move, then static
    with PiKvmClient(load_config()) as client:
        snap, settled = screen.wait_until_stable(
            client, timeout=5, poll=0.05, threshold=1.5)
    assert settled is True
    assert mock_device.kvmd.snapshot_churn == 0   # churn was consumed
    assert snap.source == "settled"


def test_wait_until_stable_static_settles_immediately(mock_device):
    with PiKvmClient(load_config()) as client:
        snap, settled = screen.wait_until_stable(client, timeout=3, poll=0.05)
    assert settled is True


# -- compound actions ---------------------------------------------------------

def test_type_with_submit_presses_enter(mock_device):
    with PiKvmClient(load_config()) as client:
        hid.type_text(client, "ls -la", submit=True)
    events = _wait_events(mock_device, 3)
    kinds = [e["kind"] for e in events]
    assert "hid_print" in kinds
    # an Enter key press/release followed the text
    key_events = [(e["event"]["key"], e["event"]["state"]) for e in events if e["kind"] == "key"]
    assert ("Enter", True) in key_events and ("Enter", False) in key_events


def test_run_sequence_executes_in_order(mock_device):
    seq = [
        {"action": "click", "x": 100, "y": 200},
        {"action": "type", "text": "hi"},
        {"action": "key", "combo": "ctrl+s"},
    ]
    with PiKvmClient(load_config()) as client:
        results = hid.run_sequence(client, seq)
    assert results == ["left click", "type 2 chars", "key ctrl+s"]
    events = _wait_events(mock_device, 6)
    kinds = [e["kind"] for e in events]
    # click (move+button+button), then hid_print, then ctrl+s keys
    assert kinds[0] == "mouse_move"
    assert "hid_print" in kinds
    assert kinds[-1] == "key"


def test_run_sequence_dangerous_step_blocks(mock_device):
    from pikvm_agent import guard
    seq = [
        {"action": "type", "text": "echo hi"},
        {"action": "type", "text": "sudo reboot"},   # dangerous -> blocks here
        {"action": "key", "combo": "enter"},
    ]
    with PiKvmClient(load_config()) as client:
        with pytest.raises(guard.GuardError):
            hid.run_sequence(client, seq)
