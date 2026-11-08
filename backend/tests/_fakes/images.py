"""Real images, as small as they come, for tests that send one.

A PNG signature followed by zeros used to stand in for an image: `sniff` read
the first eight bytes and nothing else. Since F410 an image must say how big it
is (`core.images.dimensions`), as every real one does, so the stand-in is a
real PNG — built here, byte by byte, with no imaging library.
"""
from __future__ import annotations

import base64
import struct
import zlib


def png(width: int = 1, height: int = 1, *, pad: int = 0) -> bytes:
    """A real `width`×`height` RGB PNG, then `pad` zero bytes (for size limits)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\x00\xff\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"") + b"\x00" * pad)


def data_uri(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()
