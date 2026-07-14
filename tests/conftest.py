import json
import socket
import threading
from pathlib import Path

import pytest
from aiohttp import web

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from mock.mock_kvmd import MockKvmd  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockDevice:
    """A mock kvmd running in a background thread for the duration of a test."""

    def __init__(self, tmp_path: Path):
        self.events_file = tmp_path / "events.jsonl"
        self.port = _free_port()
        self.kvmd = MockKvmd(events_file=self.events_file)
        self._started = threading.Event()
        self._loop = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._started.wait(10), "mock kvmd failed to start"

    def _run(self):
        import asyncio

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        runner = web.AppRunner(self.kvmd.build_app())
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        self._loop.run_until_complete(site.start())
        self._started.set()
        self._loop.run_forever()
        self._loop.run_until_complete(runner.cleanup())

    def events(self) -> list[dict]:
        if not self.events_file.exists():
            return []
        return [json.loads(line) for line in self.events_file.read_text().splitlines()]

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@pytest.fixture
def mock_device(tmp_path, monkeypatch):
    from pikvm_agent import config, screen

    device = MockDevice(tmp_path)
    monkeypatch.setenv("PIKVM_MOCK", "1")
    monkeypatch.setenv("PIKVM_MOCK_PORT", str(device.port))
    # isolate per-test state (resolution cache, screenshots, guard state) from the real dir
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(screen, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(screen, "SCREENSHOT_DIR", tmp_path / "state" / "screenshots")
    monkeypatch.setattr(screen, "RESOLUTION_CACHE", tmp_path / "state" / "resolution.json")
    monkeypatch.setattr(screen, "WAKE_ATTEMPTS", 1)  # keep fallback tests fast
    yield device
    device.stop()
