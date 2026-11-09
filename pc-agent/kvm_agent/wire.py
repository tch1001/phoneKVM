"""Wire protocol definitions for the kvm-lite PC agent.

Two kinds of WebSocket messages travel over the same connection:

* **Binary frames** -- one self-contained screen frame: a fixed 24-byte
  little-endian header followed immediately by the JPEG payload.
* **Text messages** -- UTF-8 JSON. Server -> client: ``hello``, ``stats``,
  ``ack``, ``error``. Client -> server: input events (see ``input.py``).

Binary header layout (little-endian, 24 bytes)::

    offset  size  type      field
    ------  ----  --------  -----------------------------------------------
    0       4     char[4]   magic, always ``KVM1``
    4       1     uint8     protocol version (1)
    5       1     uint8     codec (1 = JPEG, 2 = H.264 Annex-B, 3 = HEVC Annex-B)
    6       2     uint16    flags (reserved, 0)
    8       4     uint32    frame sequence number (starts at 1, wraps at 2**32)
    12      4     uint32    pixel width of the JPEG
    16      4     uint32    pixel height of the JPEG
    20      4     uint32    capture timestamp, CLOCK_MONOTONIC milliseconds,
                            truncated to 32 bits (wraps every 49.7 days)
    24      ...   uint8[]   JPEG payload (starts with FF D8, ends with FF D9)
"""

from __future__ import annotations

import struct
from typing import NamedTuple

MAGIC = b"KVM1"
PROTOCOL_VERSION = 1
CODEC_JPEG = 1
CODEC_H264 = 2
CODEC_HEVC = 3
CODEC_NAMES = {CODEC_JPEG: "jpeg", CODEC_H264: "h264", CODEC_HEVC: "hevc"}
CODEC_BY_NAME = {v: k for k, v in CODEC_NAMES.items()}
HEADER_SIZE = 24

# flags
FLAG_NONE = 0
FLAG_DUPLICATE = 1 << 0  # payload is byte-identical to the previous frame

_HEADER = struct.Struct("<4sBBHIIII")  # 24 bytes
assert _HEADER.size == HEADER_SIZE


class Header(NamedTuple):
    magic: bytes
    version: int
    codec: int
    flags: int
    seq: int
    width: int
    height: int
    timestamp_ms: int

    def as_dict(self) -> dict:
        return {
            "magic": self.magic.decode("latin-1"),
            "version": self.version,
            "codec": self.codec,
            "flags": self.flags,
            "seq": self.seq,
            "width": self.width,
            "height": self.height,
            "timestamp_ms": self.timestamp_ms,
        }


def pack_packet(seq: int, width: int, height: int, timestamp_ms: int,
                payload: bytes, codec: int = CODEC_JPEG,
                flags: int = FLAG_NONE) -> bytes:
    """Return header + encoded payload as one bytes object for send_bytes()."""
    header = _HEADER.pack(
        MAGIC, PROTOCOL_VERSION, codec, flags,
        seq & 0xFFFFFFFF, width & 0xFFFFFFFF, height & 0xFFFFFFFF,
        timestamp_ms & 0xFFFFFFFF,
    )
    return header + payload


def pack_frame(seq: int, width: int, height: int, timestamp_ms: int,
               payload: bytes, flags: int = FLAG_NONE) -> bytes:
    """JPEG frame (kept for compatibility with existing callers/tests)."""
    return pack_packet(seq, width, height, timestamp_ms, payload,
                       CODEC_JPEG, flags)


def unpack_header(buf: bytes | bytearray | memoryview) -> Header:
    """Parse the fixed header; raises ValueError on magic/version mismatch."""
    if len(buf) < HEADER_SIZE:
        raise ValueError(f"need at least {HEADER_SIZE} bytes, got {len(buf)}")
    magic, version, codec, flags, seq, width, height, ts = _HEADER.unpack_from(buf, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r} (expected {MAGIC!r})")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version {version}")
    return Header(magic, version, codec, flags, seq, width, height, ts)


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Return (width, height) parsed from a JPEG's SOF marker."""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise ValueError("not a JPEG (missing SOI marker)")
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in _SOF_MARKERS:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        if seg_len < 2:
            break
        i += 2 + seg_len
    raise ValueError("no SOF marker found")
