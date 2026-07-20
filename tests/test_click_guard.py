"""Click-target safety check: OCR wiring (click_guard.py) and its integration
into hid.click(). The real tesseract-ocr binary isn't installed in the test
environment, so the OCR call itself is stubbed for the wiring tests; a real
end-to-end test runs only when tesseract is actually available (skipped
otherwise, not failed)."""

import shutil

import pytest

from pikvm_agent import click_guard, guard, hid
from pikvm_agent.client import PiKvmClient, PiKvmError
from pikvm_agent.config import load_config


def _client() -> PiKvmClient:
    c = PiKvmClient(load_config(mock=True))
    c.ensure_auth()
    return c


# -- click_guard.read_click_region: missing-dependency handling --------------

def test_missing_pytesseract_raises_ocr_unavailable(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "pytesseract", None)  # forces ImportError on `import pytesseract`
    with pytest.raises(click_guard.OcrUnavailable, match="ocr.*extra"):
        click_guard.read_click_region(b"not a real jpeg, never reached")


def test_missing_tesseract_binary_raises_ocr_unavailable(monkeypatch):
    import pytesseract

    def _raise(*a, **k):
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, "image_to_string", _raise)

    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="JPEG")
    with pytest.raises(click_guard.OcrUnavailable, match="tesseract-ocr"):
        click_guard.read_click_region(buf.getvalue())


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract-ocr binary not installed")
def test_real_ocr_reads_rendered_text():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (240, 90), "white")
    ImageDraw.Draw(img).text((10, 30), "Install Now", fill="black")
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    text = click_guard.read_click_region(buf.getvalue())
    assert "install" in text.lower()


# -- hid.click(check_label=True): wired into the guard/confirm flow ----------

def _stub_ocr(monkeypatch, text: str):
    monkeypatch.setattr(click_guard, "read_click_region", lambda jpeg: text)


def test_check_label_blocks_a_dangerous_control(mock_device, monkeypatch):
    _stub_ocr(monkeypatch, "Install Now")
    with _client() as c:
        with pytest.raises(guard.GuardError, match="Install Now"):
            hid.click(c, "left", 400, 300, check_label=True)
    # nothing was actually clicked — the block happens before any mouse event
    assert not any(e["kind"] == "mouse_button" for e in mock_device.events())


def test_check_label_allows_a_benign_control(mock_device, monkeypatch):
    _stub_ocr(monkeypatch, "Remove from cart")
    with _client() as c:
        hid.click(c, "left", 400, 300, check_label=True)
    assert any(e["kind"] == "mouse_button" for e in mock_device.events())


def test_check_label_off_by_default_skips_ocr_entirely(mock_device, monkeypatch):
    called = []
    monkeypatch.setattr(click_guard, "read_click_region",
                        lambda jpeg: called.append(1) or "Install Now")
    with _client() as c:
        hid.click(c, "left", 400, 300)  # check_label defaults False
    assert called == []  # OCR never invoked -- no latency cost when not asked for
    assert any(e["kind"] == "mouse_button" for e in mock_device.events())


def test_check_label_confirm_token_unblocks(mock_device, monkeypatch):
    _stub_ocr(monkeypatch, "Factory Reset")
    with _client() as c:
        with pytest.raises(guard.GuardError) as ei:
            hid.click(c, "left", 500, 500, check_label=True)
        token = ei.value.token
        assert token is not None
        hid.click(c, "left", 500, 500, check_label=True, confirm_token=token)
    assert any(e["kind"] == "mouse_button" for e in mock_device.events())


def test_check_label_ocr_unavailable_propagates_not_swallowed(mock_device, monkeypatch):
    def _raise(jpeg):
        raise click_guard.OcrUnavailable("stubbed: dependency missing")
    monkeypatch.setattr(click_guard, "read_click_region", _raise)
    with _client() as c:
        with pytest.raises(click_guard.OcrUnavailable):
            hid.click(c, "left", 400, 300, check_label=True)
    # a failed check must not fall back to an unchecked click
    assert not any(e["kind"] == "mouse_button" for e in mock_device.events())


def test_check_label_ignored_without_coordinates(mock_device, monkeypatch):
    # check_label needs a target region; a coordinate-less click (acts on
    # wherever the cursor already is) has nothing to crop, so it's a no-op
    called = []
    monkeypatch.setattr(click_guard, "read_click_region", lambda jpeg: called.append(1) or "")
    with _client() as c:
        hid.click(c, "left", check_label=True)
    assert called == []
