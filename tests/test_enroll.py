"""Enrollment gateway: pairing token, auth, and network import."""

import asyncio
import socket
import threading

import httpx
import keyring
import pytest
from aiohttp import web

from pikvm_agent import config as config_mod, enroll
from tests.test_keyring import MemoryKeyring


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EnrollServer:
    def __init__(self):
        self.port = _free_port()
        self._started = threading.Event()
        self._loop = None
        threading.Thread(target=self._run, daemon=True).start()
        assert self._started.wait(10)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        runner = web.AppRunner(enroll.build_app())
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        self._loop.run_until_complete(site.start())
        self._started.set()
        self._loop.run_forever()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    prev = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    monkeypatch.setattr(config_mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config_mod, "ENROLL_TOKEN_PATH", tmp_path / "state" / "enroll_token.json")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    import pikvm_agent.guard as guard
    monkeypatch.setattr(guard.config, "STATE_DIR", tmp_path / "state")
    for var in ("PIKVM_HOST", "PIKVM_PROFILE", "PIKVM_MOCK"):
        monkeypatch.delenv(var, raising=False)
    srv = EnrollServer()
    yield srv
    srv.stop()
    keyring.set_keyring(prev)


REG = {"version": 1, "default": "my_desktop",
       "profiles": [{"name": "my_desktop", "host": "192.168.1.50", "user": "mr_robot",
                     "use_ssl": True, "password": "sekret"}]}


# -- pairing token ------------------------------------------------------------

def test_token_create_and_verify(gateway):
    assert config_mod.verify_pairing_token("anything") is False   # none yet
    token = config_mod.create_pairing_token()
    assert config_mod.verify_pairing_token(token) is True
    assert config_mod.verify_pairing_token("wrong") is False
    # only the hash is on disk, not the token
    assert token not in config_mod.ENROLL_TOKEN_PATH.read_text()


# -- endpoint auth ------------------------------------------------------------

def test_health_needs_no_auth(gateway):
    r = httpx.get(f"{gateway.url}/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_enroll_rejects_without_token(gateway):
    config_mod.create_pairing_token()
    r = httpx.post(f"{gateway.url}/enroll", json=REG)
    assert r.status_code == 401


def test_enroll_rejects_bad_token(gateway):
    config_mod.create_pairing_token()
    r = httpx.post(f"{gateway.url}/enroll", json=REG,
                   headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_enroll_accepts_valid_token_and_imports(gateway):
    token = config_mod.create_pairing_token()
    r = httpx.post(f"{gateway.url}/enroll", json=REG,
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["imported"] == ["my_desktop"]
    # profile is now usable, password landed in the keyring
    cfg = config_mod.load_config(profile="my_desktop")
    assert cfg.host == "192.168.1.50" and cfg.password == "sekret"


def test_enroll_rejects_bad_payload(gateway):
    token = config_mod.create_pairing_token()
    r = httpx.post(f"{gateway.url}/enroll", json={"profiles": []},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400


def test_profiles_endpoint_lists_without_secrets(gateway):
    token = config_mod.create_pairing_token()
    httpx.post(f"{gateway.url}/enroll", json=REG,
               headers={"Authorization": f"Bearer {token}"})
    r = httpx.get(f"{gateway.url}/profiles", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == "my_desktop"
    assert body["profiles"][0]["host"] == "192.168.1.50"
    assert "password" not in body["profiles"][0]
