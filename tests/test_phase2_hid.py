"""Phase 2: keymap, pixel->kvm coordinate math, mouse/keyboard event sequences."""

import time

import pytest

from pikvm_agent import hid, keymap
from pikvm_agent.client import PiKvmClient
from pikvm_agent.config import load_config


# -- pure unit tests -----------------------------------------------------------

def test_pixel_to_kvm_corners_and_center():
    w, h = 1280, 720
    assert hid.pixel_to_kvm(0, 0, w, h) == (-32768, -32768)
    kx, ky = hid.pixel_to_kvm(w, h, w, h)  # bottom-right edge clamps to max
    assert (kx, ky) == (32767, 32767)
    cx, cy = hid.pixel_to_kvm(w / 2, h / 2, w, h)
    assert abs(cx) <= 1 and abs(cy) <= 1


def test_keymap_chars():
    assert keymap.from_char("a") == keymap.MappedKey("KeyA", False)
    assert keymap.from_char("Z") == keymap.MappedKey("KeyZ", True)
    assert keymap.from_char("7") == keymap.MappedKey("Digit7", False)
    assert keymap.from_char("?") == keymap.MappedKey("Slash", True)
    assert keymap.from_char("\n") == keymap.MappedKey("Enter", False)
    assert keymap.from_char("é") is None


def test_parse_combo():
    assert keymap.parse_combo("ctrl+alt+del") == ["ControlLeft", "AltLeft", "Delete"]
    assert keymap.parse_combo("meta") == ["MetaLeft"]
    assert keymap.parse_combo("ctrl+shift+t") == ["ControlLeft", "ShiftLeft", "KeyT"]
    assert keymap.parse_combo("F5") == ["F5"]
    with pytest.raises(ValueError):
        keymap.parse_combo("ctrl+")


# -- event-sequence tests against the mock --------------------------------------

def _wait_events(mock_device, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = mock_device.events()
        if len(events) >= n:
            return events
        time.sleep(0.05)
    raise AssertionError(f"expected {n} events, got {mock_device.events()}")


def test_click_at_coordinates(mock_device):
    with PiKvmClient(load_config()) as client:
        hid.click(client, "left", 100, 200)
    events = _wait_events(mock_device, 3)
    assert [e["kind"] for e in events] == ["mouse_move", "mouse_button", "mouse_button"]
    # mock frame is 1280x720
    assert events[0]["event"]["to"] == {
        "x": hid.to_kvm(100, 1280), "y": hid.to_kvm(200, 720)}
    assert events[1]["event"] == {"button": "left", "state": True}
    assert events[2]["event"] == {"button": "left", "state": False}


def test_type_uses_hid_print(mock_device):
    with PiKvmClient(load_config()) as client:
        assert hid.type_text(client, "Hello") == "print"
    events = _wait_events(mock_device, 1)
    assert events[-1]["kind"] == "hid_print"
    assert events[-1]["text"] == "Hello"


def test_type_ws_fallback_shift_wrapping(mock_device):
    with PiKvmClient(load_config()) as client:
        assert hid.type_text(client, "Hi", force_ws=True) == "ws"
    events = _wait_events(mock_device, 6)
    seq = [(e["event"]["key"], e["event"]["state"]) for e in events if e["kind"] == "key"]
    assert seq == [
        ("ShiftLeft", True), ("KeyH", True), ("KeyH", False), ("ShiftLeft", False),
        ("KeyI", True), ("KeyI", False),
    ]


def test_combo_press_release_order(mock_device):
    with PiKvmClient(load_config()) as client:
        hid.press_combo(client, "ctrl+s")
    events = _wait_events(mock_device, 4)
    seq = [(e["event"]["key"], e["event"]["state"]) for e in events]
    assert seq == [
        ("ControlLeft", True), ("KeyS", True),
        ("KeyS", False), ("ControlLeft", False),
    ]


def test_wheel(mock_device):
    with PiKvmClient(load_config()) as client:
        hid.wheel(client, -3)
    events = _wait_events(mock_device, 1)
    assert events[-1]["kind"] == "mouse_wheel"
    assert events[-1]["event"]["delta"] == {"x": 0, "y": -3}
