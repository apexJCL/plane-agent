"""Files an agent attaches to a signed comment (agent-coordination, "Screenshots and attachments").

Every check here runs before any request. The type comes from the CLI's own extension table, not `mimetypes`,
which depends on the host. Each type is in Plane's ATTACHMENT_MIME_TYPES; SVG, HTML, JS and CSS are left out
because the tracker serves attachments from its own origin.
"""

from __future__ import annotations

import html
import os
import struct
from dataclasses import dataclass

INLINE = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
TYPES = {
    **INLINE,
    "pdf": "application/pdf",
    **dict.fromkeys(["txt", "log", "yaml", "yml", "diff", "patch"], "text/plain"),
    "md": "text/markdown",
    "json": "application/json",
    "csv": "text/csv",
    "zip": "application/zip",
    "gz": "application/gzip",
    "tgz": "application/gzip",
    "tar": "application/x-tar",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
}
MAX_BYTES = 26_214_400  # the tracker's FILE_SIZE_LIMIT (deploy/plane/docker-compose.yml)
MAX_WIDTH = 720  # inline images wider than this are shown scaled down


class FileRefused(ValueError):
    """A file failed a local check; the command makes no request (exit 5)."""


@dataclass
class File:
    name: str  # basename, as sent
    type: str
    data: bytes
    dims: tuple[int, int] | None = None  # width, height of an inline image

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def inline(self) -> bool:
        return self.dims is not None

    def image_html(self, asset_id: str) -> str:
        """Plane's editor node for an image, resolved by asset id within the project."""
        w, h = self.dims  # type: ignore[misc]
        shown_w, shown_h = (MAX_WIDTH, max(1, round(h * MAX_WIDTH / w))) if w > MAX_WIDTH else (w, h)
        return (f'<image-component src="{html.escape(asset_id)}" width="{shown_w}px" height="{shown_h}px" '
                f'aspectratio="{w / h:.4f}" alignment="left"></image-component>')  # fmt: skip


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _png(d: bytes) -> tuple[int, int] | None:
    if d[:8] == b"\x89PNG\r\n\x1a\n" and d[12:16] == b"IHDR" and len(d) >= 24:
        return struct.unpack(">II", d[16:24])
    return None


def _gif(d: bytes) -> tuple[int, int] | None:
    if d[:6] in (b"GIF87a", b"GIF89a") and len(d) >= 10:
        return struct.unpack("<HH", d[6:10])
    return None


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg(d: bytes) -> tuple[int, int] | None:
    if d[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(d):
        if d[i] != 0xFF:
            return None
        marker = d[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
            i += 2
            continue
        if marker == 0xDA:  # start of scan before any frame header
            return None
        (seglen,) = struct.unpack(">H", d[i + 2 : i + 4])
        if marker in _SOF and i + 9 <= len(d):
            h, w = struct.unpack(">HH", d[i + 5 : i + 9])
            return w, h
        i += 2 + seglen
    return None


def _webp(d: bytes) -> tuple[int, int] | None:
    if d[:4] != b"RIFF" or d[8:12] != b"WEBP":
        return None
    chunk = d[12:16]
    if chunk == b"VP8 " and d[23:26] == b"\x9d\x01\x2a" and len(d) >= 30:
        w, h = struct.unpack("<HH", d[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and len(d) >= 25 and d[20] == 0x2F:
        bits = int.from_bytes(d[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(d) >= 30:
        return int.from_bytes(d[24:27], "little") + 1, int.from_bytes(d[27:30], "little") + 1
    return None


_DIMS = {"png": _png, "jpg": _jpeg, "jpeg": _jpeg, "gif": _gif, "webp": _webp}


def load(paths: list[str]) -> list[File]:
    """Read and check every file (paths relative to the current directory). Raises FileRefused on the first
    failure, before anything is sent."""
    out: list[File] = []
    for path in paths:
        name = os.path.basename(path.rstrip("/")) or path
        if not os.path.exists(path):
            raise FileRefused(f"--attach {path}: no such file")
        if not os.path.isfile(path):
            raise FileRefused(f"--attach {path}: not a regular file")
        size = os.path.getsize(path)
        if size == 0:
            raise FileRefused(f"--attach {path}: empty file")
        if size > MAX_BYTES:
            raise FileRefused(f"--attach {path}: {human_size(size)} is over the tracker's 25 MiB limit")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in TYPES:
            raise FileRefused(f"--attach {path}: .{ext or '(none)'} is not accepted; use one of "
                              f"{', '.join('.' + e for e in TYPES)}")  # fmt: skip
        if any(f.name == name for f in out):
            raise FileRefused(f"--attach {path}: another file is also named {name}")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise FileRefused(f"--attach {path}: {e.strerror or e}") from e
        dims = None
        if ext in INLINE:
            dims = _DIMS[ext](data)
            if not dims or not all(dims):
                raise FileRefused(f"--attach {path}: not a valid {ext.upper()} image")
        out.append(File(name, TYPES[ext], data, dims))
    return out
