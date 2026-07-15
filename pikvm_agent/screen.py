"""Screen capture: one-shot JPEG snapshots of the KVM target.

Primary path is kvmd's snapshot endpoint; if that is unavailable (e.g.
the MJPEG streamer is suspended in Janus/H264-only setups) we fall back
to grabbing a single frame from the /stream MJPEG multipart stream.
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .client import PiKvmClient, PiKvmError
from .config import STATE_DIR

SCREENSHOT_DIR = STATE_DIR / "screenshots"
RESOLUTION_CACHE = STATE_DIR / "resolution.json"

WAKE_ATTEMPTS = 10  # 1s apart, while holding /api/ws open to resume the streamer

# Defaults for wait_until_stable(): a frame-diff below STABLE_THRESHOLD (mean
# per-pixel 0..255 difference of a downscaled grayscale) counts as "settled".
STABLE_THRESHOLD = 1.5
STABLE_POLL = 0.4
STABLE_TIMEOUT = 8.0


@dataclass
class Snapshot:
    jpeg: bytes
    width: int
    height: int
    source: str  # "snapshot" | "stream" | "snapshot-wake"
    crop: tuple[int, int, int, int] | None = None  # (x, y, w, h) in full-screen px

    def save(self, path: Path | None = None) -> Path:
        if path is None:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = SCREENSHOT_DIR / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.jpeg)
        return path


def _dimensions(jpeg: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(jpeg)) as img:
        return img.size


def _save_resolution(width: int, height: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RESOLUTION_CACHE.write_text(json.dumps({"width": width, "height": height}))


def _try_snapshot(client: PiKvmClient) -> bytes | None:
    try:
        body, content_type = client.get_bytes("/api/streamer/snapshot")
    except PiKvmError:
        return None
    return body if content_type.startswith("image/") else None


def _crop_jpeg(jpeg: bytes, crop: tuple[int, int, int, int]) -> bytes:
    """Crop (x, y, w, h) from a JPEG, re-encoding at native resolution.

    Cropping keeps full pixel fidelity of the region (unlike downscaling) while
    cutting the image-token cost roughly in proportion to the area kept.
    """
    x, y, w, h = crop
    with Image.open(io.BytesIO(jpeg)) as img:
        region = img.crop((x, y, x + w, y + h))
        out = io.BytesIO()
        region.convert("RGB").save(out, format="JPEG", quality=90)
        return out.getvalue()


def capture(client: PiKvmClient, crop: tuple[int, int, int, int] | None = None) -> Snapshot:
    """Capture the screen. `crop` = (x, y, w, h) in full-screen pixels returns
    just that region at native resolution (coordinates elsewhere still refer to
    the full screen; the resolution cache always holds the full dimensions)."""
    body = _try_snapshot(client)
    source = "snapshot"

    if body is None:
        # kvmd suspends the streamer when no KVM client is connected and
        # snapshot answers 503. Holding the /api/ws event socket open makes
        # kvmd resume the streamer (verified on kvmd 4.132), so wake it and
        # retry briefly.
        try:
            with client.open_ws():
                for _ in range(WAKE_ATTEMPTS):
                    time.sleep(1)
                    body = _try_snapshot(client)
                    if body is not None:
                        source = "snapshot-wake"
                        break
        except Exception:
            pass

    if body is None:
        try:
            body = _grab_stream_frame(client)
            source = "stream"
        except Exception as stream_err:
            raise PiKvmError(
                "no screen capture available: snapshot failed (also after "
                f"waking the streamer via /api/ws); stream fallback failed ({stream_err}). "
                "On the PiKVM, `systemctl start kvmd-streamer` may be needed."
            ) from None

    full_w, full_h = _dimensions(body)
    _save_resolution(full_w, full_h)

    if crop is not None:
        body = _crop_jpeg(body, crop)
        w, h = _dimensions(body)
        return Snapshot(jpeg=body, width=w, height=h, source=source, crop=crop)
    return Snapshot(jpeg=body, width=full_w, height=full_h, source=source)


def _frame_diff(jpeg_a: bytes, jpeg_b: bytes) -> float:
    """Mean per-pixel difference (0..255) of two frames, downscaled to a small
    grayscale thumbnail. Cheap motion metric — ~0 when the screen is static."""
    from PIL import ImageChops, ImageStat

    with Image.open(io.BytesIO(jpeg_a)) as a, Image.open(io.BytesIO(jpeg_b)) as b:
        ta = a.convert("L").resize((64, 36))
        tb = b.convert("L").resize((64, 36))
        return ImageStat.Stat(ImageChops.difference(ta, tb)).mean[0]


def wait_until_stable(
    client: PiKvmClient,
    timeout: float = STABLE_TIMEOUT,
    poll: float = STABLE_POLL,
    threshold: float = STABLE_THRESHOLD,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[Snapshot, bool]:
    """Poll frames until the screen stops changing, then return a snapshot.

    Replaces blind `sleep` after an action: returns as soon as two consecutive
    frames differ by less than `threshold`. Returns (snapshot, settled) where
    `settled` is False if `timeout` elapsed while still changing.
    """
    deadline = time.monotonic() + timeout
    settled = False
    prev: bytes | None = None
    # Keep ONE socket open for the whole loop: kvmd suspends the streamer the
    # moment no client is attached, so a fresh socket per poll would race the
    # re-suspend and keep getting 503s (seen live on kvmd 4.132).
    with client.open_ws():
        for _ in range(WAKE_ATTEMPTS):  # first frame, waking the streamer if needed
            prev = _try_snapshot(client)
            if prev is not None:
                break
            time.sleep(1)
        while prev is not None and time.monotonic() < deadline:
            time.sleep(poll)
            cur = _try_snapshot(client)
            if cur is None:
                continue
            if _frame_diff(prev, cur) < threshold:
                settled = True
                prev = cur
                break
            prev = cur

    if prev is None:  # streamer never woke — fall back to the full capture path
        prev = capture(client).jpeg

    full_w, full_h = _dimensions(prev)
    _save_resolution(full_w, full_h)
    if crop is not None:
        cropped = _crop_jpeg(prev, crop)
        cw, ch = _dimensions(cropped)
        return Snapshot(cropped, cw, ch, "settled" if settled else "unsettled", crop), settled
    return Snapshot(prev, full_w, full_h, "settled" if settled else "unsettled"), settled


def capture_ocr(client: PiKvmClient) -> str:
    """Server-side OCR of the current frame (Tesseract on the PiKVM)."""
    body, content_type = client.get_bytes("/api/streamer/snapshot", ocr=1)
    if content_type.startswith("image/"):
        raise PiKvmError("device returned an image; OCR not supported by this kvmd")
    return body.decode("utf-8", errors="replace")


def _grab_stream_frame(client: PiKvmClient, timeout: float = 10.0) -> bytes:
    """Read one JPEG frame from the MJPEG multipart stream."""
    with client._http.stream("GET", "/stream", timeout=timeout) as r:
        if r.status_code != 200:
            raise PiKvmError(f"GET /stream -> HTTP {r.status_code}")
        buf = b""
        content_length = None
        header_end = -1
        for chunk in r.iter_bytes():
            buf += chunk
            if content_length is None:
                header_end = buf.find(b"\r\n\r\n")
                if header_end == -1:
                    continue
                headers = buf[:header_end].decode("iso-8859-1", errors="replace").lower()
                for line in headers.splitlines():
                    if line.startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break
                if content_length is None:
                    # part headers without a length; drop this part's headers and retry
                    buf = buf[header_end + 4:]
                    continue
            start = header_end + 4
            if len(buf) >= start + content_length:
                return buf[start:start + content_length]
        raise PiKvmError("stream ended before a full frame arrived")
