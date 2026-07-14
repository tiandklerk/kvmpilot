"""Screen capture: one-shot JPEG snapshots of the KVM target.

Primary path is kvmd's snapshot endpoint; if that is unavailable (e.g.
the MJPEG streamer is suspended in Janus/H264-only setups) we fall back
to grabbing a single frame from the /stream MJPEG multipart stream.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .client import PiKvmClient, PiKvmError
from .config import STATE_DIR

SCREENSHOT_DIR = STATE_DIR / "screenshots"


@dataclass
class Snapshot:
    jpeg: bytes
    width: int
    height: int
    source: str  # "snapshot" | "stream"

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


def capture(client: PiKvmClient) -> Snapshot:
    try:
        body, content_type = client.get_bytes("/api/streamer/snapshot")
        if content_type.startswith("image/"):
            w, h = _dimensions(body)
            return Snapshot(jpeg=body, width=w, height=h, source="snapshot")
        raise PiKvmError(f"snapshot returned {content_type or 'no content-type'}")
    except PiKvmError as snapshot_err:
        try:
            body = _grab_stream_frame(client)
        except Exception as stream_err:
            raise PiKvmError(
                f"no screen capture available: snapshot failed ({snapshot_err}); "
                f"stream fallback failed ({stream_err}). "
                "On the PiKVM, `systemctl start kvmd-streamer` may be needed."
            ) from None
        w, h = _dimensions(body)
        return Snapshot(jpeg=body, width=w, height=h, source="stream")


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
