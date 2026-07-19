"""Mouse-mode detection: pixel actions require kvmd's absolute pointer mode;
a device left in a relative output (usb_rel) is auto-switched first."""

import pytest

from pikvm_agent import hid
from pikvm_agent.client import PiKvmClient, PiKvmError
from pikvm_agent.config import load_config


def _client() -> PiKvmClient:
    client = PiKvmClient(load_config(mock=True))
    client.ensure_auth()
    return client


def test_absolute_mode_is_a_noop(mock_device):
    with _client() as client:
        assert hid.ensure_absolute_mouse(client) == "absolute"
    assert [e for e in mock_device.events() if e["kind"] == "set_params"] == []


def test_relative_mode_raises_without_switching(mock_device):
    # default must NOT re-enumerate the gadget — it raises with guidance instead
    mock_device.kvmd.mouse_absolute = False
    mock_device.kvmd.mouse_output_active = "usb_rel"
    with _client() as client:
        with pytest.raises(PiKvmError, match="will not switch on its own"):
            hid.ensure_absolute_mouse(client)
    assert [e for e in mock_device.events() if e["kind"] == "set_params"] == []
    assert mock_device.kvmd.mouse_absolute is False  # untouched


def test_relative_mode_switches_only_on_explicit_optin(mock_device):
    mock_device.kvmd.mouse_absolute = False
    mock_device.kvmd.mouse_output_active = "usb_rel"
    with _client() as client:
        assert hid.ensure_absolute_mouse(client, auto_switch=True) == "switched:usb"
    switches = [e for e in mock_device.events() if e["kind"] == "set_params"]
    assert switches and switches[0]["mouse_output"] == "usb"
    assert mock_device.kvmd.mouse_absolute is True


def test_mouse_move_refuses_in_relative_mode(mock_device):
    # a normal mouse action must not silently switch — it refuses cleanly
    mock_device.kvmd.mouse_absolute = False
    mock_device.kvmd.mouse_output_active = "usb_rel"
    with _client() as client:
        with pytest.raises(PiKvmError, match="will not switch on its own"):
            hid.mouse_move(client, 100, 100)
    kinds = [e["kind"] for e in mock_device.events()]
    assert "set_params" not in kinds and "mouse_move" not in kinds


def test_result_cached_per_client(mock_device):
    with _client() as client:
        hid.ensure_absolute_mouse(client)
        # flip the mock to relative behind the client's back: the cached client
        # must not re-check (one state request per process by design)
        mock_device.kvmd.mouse_absolute = False
        assert hid.ensure_absolute_mouse(client) == "absolute"


def test_no_absolute_output_is_a_clear_error(mock_device):
    mock_device.kvmd.mouse_absolute = False
    mock_device.kvmd.mouse_output_active = "usb_rel"
    mock_device.kvmd.mouse_outputs_available = ["usb_rel"]
    with _client() as client:
        with pytest.raises(PiKvmError, match="no absolute output"):
            hid.ensure_absolute_mouse(client)
    assert [e for e in mock_device.events() if e["kind"] == "set_params"] == []
