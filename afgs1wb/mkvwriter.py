"""A minimal Matroska writer for a single VVC video track.

PyAV cannot stream-copy VVC (it looks for a VVC encoder), and a raw Annex B
elementary stream loses the presentation timestamps of B-pictures. This
writer stores the packets exactly as given (length-prefixed NAL units), the
original vvcC codec private data, and each packet's own presentation time in
milliseconds, so FFmpeg can then merge it with the other streams losslessly.
"""

from __future__ import annotations

from pathlib import Path
import os
import struct

CODEC_ID_VVC = "V_MPEGI/ISO/VVC"
_UNKNOWN_SIZE = b"\x01\xff\xff\xff\xff\xff\xff\xff"


def _id(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _size(n: int) -> bytes:
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | n).to_bytes(length, "big")
    raise ValueError("element too large")


def _element(eid: int, payload: bytes) -> bytes:
    return _id(eid) + _size(len(payload)) + payload


def _uint(eid: int, value: int) -> bytes:
    return _element(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def _string(eid: int, value: str) -> bytes:
    return _element(eid, value.encode("utf-8"))


def _float(eid: int, value: float) -> bytes:
    return _element(eid, struct.pack(">d", value))


class MatroskaVideoWriter:
    TIMESTAMP_SCALE_NS = 1_000_000          # block timestamps in milliseconds

    def __init__(self, path: str | Path, codec_private: bytes, width: int, height: int,
                 frame_duration_ns: int | None = None) -> None:
        self.path = Path(path)
        self._file = open(self.path, "wb")
        header = (_uint(0x4286, 1) + _uint(0x42F7, 1) + _uint(0x42F2, 4) + _uint(0x42F3, 8)
                  + _string(0x4282, "matroska") + _uint(0x4287, 4) + _uint(0x4285, 2))
        self._file.write(_element(0x1A45DFA3, header))
        self._file.write(_id(0x18538067))
        self._segment_size_at = self._file.tell()
        self._file.write(_UNKNOWN_SIZE)
        self._segment_start = self._file.tell()
        info = (_uint(0x2AD7B1, self.TIMESTAMP_SCALE_NS)
                + _string(0x4D80, "afgs1wb") + _string(0x5741, "afgs1wb"))
        self._duration_at = self._file.tell() + len(_id(0x1549A966)) + len(_size(len(info) + 11)) + len(info)
        self._file.write(_element(0x1549A966, info + _float(0x4489, 0.0)))
        video = _uint(0xB0, width) + _uint(0xBA, height)
        track = (_uint(0xD7, 1) + _uint(0x73C5, 1) + _uint(0x83, 1) + _uint(0x9C, 0)
                 + _string(0x86, CODEC_ID_VVC) + _element(0x63A2, codec_private)
                 + _element(0xE0, video))
        if frame_duration_ns:
            track += _uint(0x23E383, frame_duration_ns)
        self._file.write(_element(0x1654AE6B, _element(0xAE, track)))
        self._cluster_ts: int | None = None
        self._cluster = bytearray()
        self._last_end_ms = 0
        self.blocks = 0

    def write(self, data: bytes, pts_ms: int, keyframe: bool, duration_ms: int = 0) -> None:
        if pts_ms < 0:
            raise ValueError("Matroska block timestamps must not be negative")
        if (self._cluster_ts is None or keyframe or abs(pts_ms - self._cluster_ts) > 30000):
            self._flush()
            self._cluster_ts = pts_ms
        relative = pts_ms - self._cluster_ts
        block = (b"\x81" + struct.pack(">h", relative) + bytes((0x80 if keyframe else 0,)) + data)
        self._cluster += _element(0xA3, block)
        self._last_end_ms = max(self._last_end_ms, pts_ms + duration_ms)
        self.blocks += 1

    def _flush(self) -> None:
        if self._cluster_ts is None or not self._cluster:
            return
        body = _uint(0xE7, self._cluster_ts) + bytes(self._cluster)
        self._file.write(_element(0x1F43B675, body))
        self._cluster = bytearray()

    def close(self) -> None:
        if self._file.closed:
            return
        self._flush()
        end = self._file.tell()
        self._file.seek(self._segment_size_at)
        self._file.write(b"\x01" + (end - self._segment_start).to_bytes(7, "big"))
        self._file.seek(self._duration_at)
        self._file.write(_float(0x4489, float(self._last_end_ms)))
        self._file.close()

    def abort(self) -> None:
        if not self._file.closed:
            self._file.close()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
