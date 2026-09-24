"""Decoding exact frames with ffmpeg into NumPy planes or raw planar files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .probe import VideoInfo
from .tools import CancelToken, run


@dataclass(frozen=True)
class Geometry:
    """Planar layout frames are converted to (normally the encode's own)."""
    width: int
    height: int
    bit_depth: int
    subsampling_x: int
    subsampling_y: int

    @classmethod
    def of(cls, info: VideoInfo) -> "Geometry":
        return cls(info.width, info.height, info.bit_depth, info.subsampling_x, info.subsampling_y)

    @property
    def pix_fmt(self) -> str:
        chroma = {(1, 1): "420", (1, 0): "422", (0, 0): "444"}[(self.subsampling_x, self.subsampling_y)]
        return f"yuv{chroma}p" + (f"{self.bit_depth}le" if self.bit_depth > 8 else "")

    @property
    def chroma_shape(self) -> tuple[int, int]:
        return ((self.height + self.subsampling_y) >> self.subsampling_y,
                (self.width + self.subsampling_x) >> self.subsampling_x)

    @property
    def sample_bytes(self) -> int:
        return 2 if self.bit_depth > 8 else 1

    @property
    def frame_bytes(self) -> int:
        ch, cw = self.chroma_shape
        return (self.width * self.height + 2 * ch * cw) * self.sample_bytes


@dataclass(frozen=True)
class Crop:
    width: int
    height: int
    x: int
    y: int


@dataclass
class Frame:
    index: int
    y: np.ndarray
    u: np.ndarray
    v: np.ndarray
    geometry: Geometry


def source_crop_for(source: VideoInfo, encoded: VideoInfo) -> Crop | None:
    """Centre crop that gives the source the encode's aspect ratio (letterbox removal)."""
    src_ar = source.width / source.height
    enc_ar = encoded.width / encoded.height
    if abs(src_ar - enc_ar) < 0.01:
        return None
    if enc_ar > src_ar:          # encode is wider: crop source top/bottom
        h = int(round(source.width / enc_ar / 2)) * 2
        return Crop(source.width, h, 0, (source.height - h) // 2 // 2 * 2)
    w = int(round(source.height * enc_ar / 2)) * 2
    return Crop(w, source.height, (source.width - w) // 2 // 2 * 2, 0)


def _filters(info: VideoInfo, target: Geometry, crop: Crop | None) -> str:
    parts = []
    if crop:
        parts.append(f"crop={crop.width}:{crop.height}:{crop.x}:{crop.y}")
    width, height = (crop.width, crop.height) if crop else (info.width, info.height)
    if (width, height) != (target.width, target.height):
        parts.append(f"scale={target.width}:{target.height}:flags=spline+accurate_rnd+full_chroma_int")
    parts.append(f"format={target.pix_fmt}")
    return ",".join(parts)


def _command(ffmpeg: str, info: VideoInfo, index: int, count: int, target: Geometry,
             crop: Crop | None, output: str) -> list[str]:
    # Seek to half a frame before the wanted frame on the absolute timeline;
    # accurate seeking then drops everything earlier, so the first frame
    # returned is exactly ``index`` even if timestamps are rounded to 1 ms.
    seconds = info.first_pts + (index - 0.5) / float(info.fps)
    return [ffmpeg, "-v", "error", "-nostdin", "-y", "-seek_timestamp", "1",
            "-ss", f"{max(0.0, seconds):.6f}", "-i", str(info.path),
            "-map", f"0:{info.stream_index}", "-frames:v", str(count),
            "-an", "-sn", "-dn", "-vf", _filters(info, target, crop),
            "-f", "rawvideo", "-pix_fmt", target.pix_fmt, output]


def split_planes(buffer: bytes | np.ndarray, geometry: Geometry) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    dtype = np.uint16 if geometry.bit_depth > 8 else np.uint8
    flat = np.frombuffer(buffer, dtype=dtype) if isinstance(buffer, (bytes, bytearray)) else buffer
    ch, cw = geometry.chroma_shape
    ny = geometry.width * geometry.height
    y = flat[:ny].reshape(geometry.height, geometry.width)
    u = flat[ny:ny + ch * cw].reshape(ch, cw)
    v = flat[ny + ch * cw:ny + 2 * ch * cw].reshape(ch, cw)
    return y, u, v


def read_frames(ffmpeg: str, info: VideoInfo, index: int, count: int,
                cancel: CancelToken | None = None, target: Geometry | None = None,
                crop: Crop | None = None) -> list[Frame]:
    """Decode ``count`` consecutive frames starting at ``index`` (one seek)."""
    target = target or Geometry.of(info)
    data = run(_command(ffmpeg, info, index, count, target, crop, "pipe:1"), cancel)
    n = len(data) // target.frame_bytes
    if n == 0:
        raise RuntimeError(f"{info.path.name}: frame {index} could not be decoded")
    frames = []
    for k in range(n):
        y, u, v = split_planes(data[k * target.frame_bytes:(k + 1) * target.frame_bytes], target)
        frames.append(Frame(index + k, y.copy(), u.copy(), v.copy(), target))
    return frames


def read_frame(ffmpeg: str, info: VideoInfo, index: int, cancel: CancelToken | None = None,
               target: Geometry | None = None, crop: Crop | None = None) -> Frame:
    return read_frames(ffmpeg, info, index, 1, cancel, target, crop)[0]


def extract_raw(ffmpeg: str, info: VideoInfo, index: int, count: int, output: Path,
                cancel: CancelToken | None = None, target: Geometry | None = None,
                crop: Crop | None = None) -> int:
    """Write ``count`` consecutive frames as raw planar YUV; return frames written."""
    target = target or Geometry.of(info)
    run(_command(ffmpeg, info, index, count, target, crop, str(output)), cancel)
    return output.stat().st_size // target.frame_bytes if output.exists() else 0


def luma_planes(path: Path, geometry: Geometry, count: int, step: int = 1) -> np.ndarray:
    """Memory-map the Y planes of a raw file as float32 (optionally subsampled)."""
    dtype = np.uint16 if geometry.bit_depth > 8 else np.uint8
    frame_samples = geometry.frame_bytes // geometry.sample_bytes
    data = np.memmap(path, dtype=dtype, mode="r", shape=(count, frame_samples))
    ny = geometry.width * geometry.height
    return np.stack([data[i, :ny].reshape(geometry.height, geometry.width)[::step, ::step]
                     .astype(np.float32) for i in range(count)])
