"""Phase 0: config, auth, basic client calls, CLI status, WS round-trip."""

import pytest

from pikvm_agent.cli import main
from pikvm_agent.client import PiKvmClient, PiKvmError
from pikvm_agent.config import Config, load_config


def test_config_base_url_defaults():
    c = Config(host="pikvm.local", user="u", password="p")
    assert c.base_url == "https://pikvm.local"
    assert c.ws_url == "wss://pikvm.local/api/ws"


def test_config_base_url_custom_port():
    c = Config(host="10.0.0.5", user="u", password="p", use_ssl=False, port=8686)
    assert c.base_url == "http://10.0.0.5:8686"
    assert c.ws_url == "ws://10.0.0.5:8686/api/ws"


def test_load_config_mock(mock_device):
    c = load_config()
    assert not c.use_ssl
    assert c.port == mock_device.port


def test_header_auth_and_info(mock_device):
    with PiKvmClient(load_config()) as client:
        client.ensure_auth()
        info = client.info()
        assert info["meta"]["server"]["host"] == "mock-pikvm"


def test_bad_credentials_rejected(mock_device):
    config = load_config()
    config.password = "wrong"
    with PiKvmClient(config) as client:
        with pytest.raises(PiKvmError):
            client.ensure_auth()


def test_cookie_login_fallback(mock_device):
    config = load_config()
    with PiKvmClient(config) as client:
        # Simulate header auth being rejected by stripping the headers;
        # ensure_auth should fall back to POST /api/auth/login + cookie.
        client._http.headers.pop("X-KVMD-User")
        client._http.headers.pop("X-KVMD-Passwd")
        client.ensure_auth()
        assert client._cookie_auth
        assert client.info()["meta"]["server"]["host"] == "mock-pikvm"


def test_atx_state(mock_device):
    with PiKvmClient(load_config()) as client:
        atx = client.atx_state()
        assert atx["enabled"] is True
        assert atx["leds"]["power"] is True


def test_ws_events_recorded(mock_device):
    with PiKvmClient(load_config()) as client:
        with client.open_ws() as ws:
            ws.mouse_move(-32768, 32767)
            ws.mouse_button("left", True)
            ws.mouse_button("left", False)
            ws.key("KeyA", True)
            ws.key("KeyA", False)
        import time

        deadline = time.time() + 5
        while time.time() < deadline and len(mock_device.events()) < 5:
            time.sleep(0.05)
    events = mock_device.events()
    assert [e["kind"] for e in events] == ["mouse_move", "mouse_button", "mouse_button", "key", "key"]
    assert events[0]["event"]["to"] == {"x": -32768, "y": 32767}
    assert events[3]["event"] == {"key": "KeyA", "state": True}


def test_hid_print_recorded(mock_device):
    with PiKvmClient(load_config()) as client:
        client.hid_print("hello mock")
    assert mock_device.events()[-1] == pytest.approx(mock_device.events()[-1])  # sanity
    assert mock_device.events()[-1]["kind"] == "hid_print"
    assert mock_device.events()[-1]["text"] == "hello mock"


def test_cli_status_mock(mock_device, capsys):
    assert main(["--mock", "status"]) == 0
    out = capsys.readouterr().out
    assert "mock-pikvm" in out
    assert "snapshot:     available" in out
