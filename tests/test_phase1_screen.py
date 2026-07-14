"""Phase 1: snapshot capture, OCR passthrough, MJPEG single-frame fallback."""

import pytest

from pikvm_agent import screen
from pikvm_agent.cli import main
from pikvm_agent.client import PiKvmClient, PiKvmError
from pikvm_agent.config import load_config


def test_capture_snapshot(mock_device):
    with PiKvmClient(load_config()) as client:
        snap = screen.capture(client)
    assert snap.source == "snapshot"
    assert (snap.width, snap.height) == (1280, 720)
    assert snap.jpeg[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_capture_ocr(mock_device):
    with PiKvmClient(load_config()) as client:
        text = screen.capture_ocr(client)
    assert "mock" in text.lower()


def test_capture_falls_back_to_stream(mock_device):
    mock_device.kvmd.snapshot_enabled = False
    with PiKvmClient(load_config()) as client:
        snap = screen.capture(client)
    assert snap.source == "stream"
    assert (snap.width, snap.height) == (1280, 720)


def test_capture_error_when_nothing_available(mock_device):
    mock_device.kvmd.snapshot_enabled = False
    with PiKvmClient(load_config()) as client:
        # break the stream too by pointing the client at a bogus path
        orig = client._http.stream

        def broken_stream(method, path, **kw):
            return orig(method, "/nonexistent", **kw)

        client._http.stream = broken_stream
        with pytest.raises(PiKvmError, match="kvmd-streamer"):
            screen.capture(client)


def test_snapshot_save_and_cli(mock_device, tmp_path, capsys):
    out = tmp_path / "shot.jpg"
    assert main(["--mock", "screenshot", "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert str(out) in printed
    assert "1280x720" in printed
    assert out.stat().st_size > 1000
