"""Minimal PNG writer, so screenshots need no image library.

GDI hands us a raw 32-bpp bitmap; PNG wants zlib-compressed, filter-tagged
scanlines. That is about thirty lines on top of `zlib`, versus a Pillow
dependency (compiled wheel) for one function. Filter type 0 (None) everywhere:
screenshots compress well enough, and doing per-row heuristic filtering in pure
Python would cost more than it saves.
"""
from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: 8 bits per channel, truecolour (no alpha), the only thing we ever emit.
_COLOR_TYPE_RGB = 2


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def encode(width: int, height: int, rgb: bytes, compress_level: int = 6) -> bytes:
    """Encode top-down RGB bytes as a PNG. `len(rgb)` must be width*height*3."""
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"expected {expected} bytes of RGB data, got {len(rgb)}")
    stride = width * 3
    # Each scanline is prefixed with its filter byte (0 = no filtering).
    raw = bytearray((stride + 1) * height)
    for y in range(height):
        start = y * (stride + 1)
        raw[start] = 0
        raw[start + 1:start + 1 + stride] = rgb[y * stride:(y + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, _COLOR_TYPE_RGB, 0, 0, 0)
    return b"".join((
        _SIGNATURE,
        _chunk(b"IHDR", header),
        _chunk(b"IDAT", zlib.compress(bytes(raw), compress_level)),
        _chunk(b"IEND", b""),
    ))
