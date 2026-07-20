"""Optional click-target safety check: OCR a small crop around a click point
before clicking, so a destructive-looking GUI control (an "Install Now" that
restarts the machine, a "Format Disk", a delete confirmation) can be caught the
same way a dangerous typed command is — instead of sailing through unguarded,
which is what every mouse action does otherwise (guard.py only scans typed text
and key combos; a click carries no text of its own to scan).

Needs the optional `ocr` extra (`pip install -e ".[ocr]"`, i.e. pytesseract)
AND the tesseract-ocr system binary. Both are optional, so most mouse actions
never touch this module — but a caller that explicitly asks for the check
(hid.click(check_label=True)) gets a clear error if it can't actually be
performed, never a silent skip that would look identical to "checked and
clean". This is a heuristic, best-effort layer, not a guarantee: it only
catches destructive actions that are labeled in text near the click point —
an icon-only button, a non-English label, or a control just outside the crop
region all pass through unseen. Treat it as one more layer, not the boundary.
"""

from __future__ import annotations

import io


class OcrUnavailable(Exception):
    """The click-label check was requested but can't actually be performed
    (pytesseract not installed, or the tesseract-ocr binary isn't on PATH)."""


def read_click_region(jpeg: bytes) -> str:
    """OCR a (typically small, pre-cropped) frame; returns the text found.
    Raises OcrUnavailable rather than returning empty text on a missing
    dependency — silently returning "" would look identical to "checked, no
    dangerous text found" and give false confidence."""
    try:
        import pytesseract
    except ImportError as e:
        raise OcrUnavailable(
            "click-label check needs the 'ocr' extra: pip install -e '.[ocr]' "
            "(pytesseract) plus the tesseract-ocr system package"
        ) from e

    from PIL import Image

    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            return pytesseract.image_to_string(img.convert("L"))
    except pytesseract.TesseractNotFoundError as e:
        raise OcrUnavailable(
            "click-label check needs the tesseract-ocr system package "
            "(e.g. `apt install tesseract-ocr` / `brew install tesseract`)"
        ) from e
