"""Video stream properties from ffprobe, including the true first presentation time."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import json
import re

from .tools import CancelToken, ToolError, run_text

_PIX_FMT = re.compile(r"^(?:yuv|yuvj)(420|422|444)p(?:(9|10|12|14|16)(le|be))?$")


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    stream_index: int
    codec: str
    width: int
    height: int
    fps: Fraction
    pix_fmt: str
    bit_depth: int
    subsampling_x: int
    subsampling_y: int
    duration: float
    first_pts: float
    color_range: str
    matrix: str
    transfer: str
    primaries: str
    start_time: float = 0.0
    frames: int = 0

    @property
    def frame_count(self) -> int:
        """Pictures in the stream: the muxer's count when known, else duration × fps.

        The container duration is often the audio's, which can run past the
        video (a cut encode reported 2,145 frames for its 2,128 pictures).
        """
        if self.frames > 0:
            return self.frames
        return max(1, int(round(self.duration * float(self.fps))))

    @property
    def limited_range(self) -> bool:
        return self.color_range != "pc"

    @property
    def is_hdr(self) -> bool:
        return self.transfer in ("smpte2084", "arib-std-b67")

    def time_of(self, index: int) -> float:
        """Content time (seconds from the first shown frame) of frame ``index``."""
        return index / float(self.fps)

    def describe(self) -> str:
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(int(minutes), 60)
        length = f"{hours}:{minutes:02d}:{seconds:05.2f}" if hours else f"{minutes}:{seconds:05.2f}"
        hdr = " HDR" if self.is_hdr else ""
        return (f"{self.codec.upper()} {self.width}×{self.height} {float(self.fps):.3f} fps, "
                f"{self.bit_depth}-bit {self.pix_fmt}{hdr}, {length} ({self.frame_count:,} frames)")


def _first_presentation_time(ffprobe: str, path: Path, stream: int, cancel) -> float:
    """Smallest PTS among the first packets.

    ffprobe's start_time is the PTS of the first packet in *decode* order,
    which for B-picture streams can be later than the first frame shown
    (FFmpeg's own libvvenc MKVs report 0.958 s for a stream that starts at
    0). Seeking relative to it lands on the wrong frame.
    """
    out = run_text([ffprobe, "-v", "error", "-select_streams", str(stream),
                    "-read_intervals", "%+#96", "-show_entries", "packet=pts_time",
                    "-of", "csv=p=0", str(path)], cancel)
    values = [float(v) for v in out.split() if v.strip() and v.strip() != "N/A"]
    if not values:
        raise ToolError(f"{path.name}: no packet timestamps found")
    return min(values)


def probe(path: str | Path, ffprobe: str, cancel: CancelToken | None = None) -> VideoInfo:
    path = Path(path)
    if not path.is_file():
        raise ToolError(f"File not found: {path}")
    data = json.loads(run_text([ffprobe, "-v", "error", "-show_streams", "-show_format",
                                "-of", "json", str(path)], cancel))
    videos = [s for s in data.get("streams", [])
              if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ToolError(f"{path.name} has no video stream")
    s = videos[0]
    match = _PIX_FMT.match(s.get("pix_fmt", ""))
    if not match:
        raise ToolError(f"{path.name}: unsupported pixel format {s.get('pix_fmt')!r} "
                        "(planar YUV 4:2:0, 4:2:2 or 4:4:4 required)")
    chroma, depth = match.group(1), int(match.group(2) or 8)
    if depth > 12:
        raise ToolError(f"{path.name}: {depth}-bit video is not supported (max 12-bit)")
    fps = Fraction(s.get("avg_frame_rate") or "0/1") if s.get("avg_frame_rate") != "0/0" else Fraction(0)
    if fps <= 0:
        fps = Fraction(s.get("r_frame_rate") or "0/1")
    if fps <= 0:
        raise ToolError(f"{path.name}: frame rate unknown")
    fps = fps.limit_denominator(1001000)
    tags = {k.upper(): v for k, v in (s.get("tags") or {}).items()}
    duration = float(s.get("duration") or _tag_seconds(tags.get("DURATION"))
                     or data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ToolError(f"{path.name}: duration unknown")
    index = int(s["index"])
    return VideoInfo(
        path=path, stream_index=index, codec=s.get("codec_name", "?"),
        width=int(s["width"]), height=int(s["height"]), fps=fps, pix_fmt=s["pix_fmt"],
        bit_depth=depth, subsampling_x=int(chroma != "444"), subsampling_y=int(chroma == "420"),
        duration=duration, first_pts=_first_presentation_time(ffprobe, path, index, cancel),
        color_range=s.get("color_range", "tv") or "tv", matrix=s.get("color_space", "") or "",
        transfer=s.get("color_transfer", "") or "", primaries=s.get("color_primaries", "") or "",
        start_time=float(data.get("format", {}).get("start_time") or 0),
        frames=_frame_count(s, tags))


def _tag_seconds(value: str | None) -> float:
    """Matroska statistics DURATION tag ("01:29:04.955000000") in seconds."""
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", (value or "").strip())
    if not match:
        return 0.0
    h, m, sec = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def _frame_count(stream: dict, tags: dict) -> int:
    """Exact picture count from the stream header or mkvmerge's statistics tags."""
    values = [stream.get("nb_frames")] + [v for k, v in tags.items() if k.startswith("NUMBER_OF_FRAMES")]
    for value in values:
        try:
            if int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            pass
    return 0
