"""FFmpeg/ffprobe helpers used by the desktop workbench."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    bit_depth: int
    pixel_format: str
    subsampling_x: int
    subsampling_y: int
    duration: float
    codec: str
    color_primaries: str = "unknown"
    transfer: str = "unknown"
    matrix: str = "unknown"
    color_range: str = "unknown"
    start_time: float = 0.0

    @property
    def fps_text(self) -> str:
        return f"{self.fps.numerator}/{self.fps.denominator}"


def _run(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=_NO_WINDOW)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Could not find executable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(detail[-4000:]) from exc


def probe_video(path: str | Path, ffprobe: str) -> VideoInfo:
    video = Path(path).expanduser().resolve()
    if not video.is_file():
        raise RuntimeError(f"Video file not found: {video}")
    result = _run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                   "-of", "json", str(video)])
    payload = json.loads(result.stdout)
    streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise RuntimeError(f"No video stream found in {video.name}")
    stream = streams[0]
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        fps = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0, 1)
    if fps <= 0:
        raise RuntimeError(f"Could not determine the frame rate of {video.name}")
    pix_fmt = stream.get("pix_fmt", "")
    depth_match = re.search(r"p(9|10|12|14|16)(?:le|be)?$", pix_fmt)
    depth = int(depth_match.group(1)) if depth_match else 8
    if depth not in (8, 10, 12):
        raise RuntimeError(f"Unsupported pixel depth for analysis: {pix_fmt} ({depth}-bit)")
    if "444" in pix_fmt:
        sub_x, sub_y = 0, 0
    elif "422" in pix_fmt:
        sub_x, sub_y = 1, 0
    elif "420" in pix_fmt:
        sub_x, sub_y = 1, 1
    else:
        raise RuntimeError(f"Unsupported pixel format for AOM analysis: {pix_fmt}")
    duration = float(stream.get("duration") or payload.get("format", {}).get("duration") or 0)
    if duration <= 0 and stream.get("nb_frames"):
        try:
            duration = int(stream["nb_frames"]) / float(fps)
        except (ValueError, ZeroDivisionError):
            duration = 0
    return VideoInfo(video, int(stream["width"]), int(stream["height"]), fps,
                     depth, pix_fmt, sub_x, sub_y, duration,
                     stream.get("codec_name", "unknown"),
                     stream.get("color_primaries", "unknown"),
                     stream.get("color_transfer", "unknown"),
                     stream.get("color_space", "unknown"),
                     stream.get("color_range", "unknown"),
                     float(stream.get("start_time") or 0.0))


def analysis_pixel_format(info: VideoInfo) -> str:
    chroma = "420" if (info.subsampling_x, info.subsampling_y) == (1, 1) else \
             "422" if (info.subsampling_x, info.subsampling_y) == (1, 0) else "444"
    return f"yuv{chroma}p{info.bit_depth}le" if info.bit_depth > 8 else f"yuv{chroma}p"


def _analysis_rate(info: VideoInfo, max_frames: int) -> tuple[Fraction, int]:
    if info.duration <= 0:
        return info.fps, 1
    estimated = float(info.fps) * info.duration
    stride = max(1, math.ceil(estimated / max(1, max_frames)))
    return info.fps / stride, stride


def _extract_raw(
    ffmpeg: str,
    info: VideoInfo,
    target: VideoInfo,
    output_path: Path,
    frame_offset: int,
    max_frames: int,
    rate: Fraction,
    progress: Callable[[int, str], None] | None = None,
) -> None:
    pix_fmt = analysis_pixel_format(target)
    offset = max(0, frame_offset)
    first_sample_time = info.start_time + offset / float(info.fps)
    sample_count = max_frames
    if info.duration > 0:
        remaining = max(0.0, info.duration - offset / float(info.fps))
        sample_count = min(max_frames, max(0, math.ceil(remaining * float(rate))))

    vf = (f"trim=start_frame={offset},setpts=PTS-STARTPTS,"
          f"fps={rate.numerator}/{rate.denominator},"
          f"scale={target.width}:{target.height}:flags=lanczos,format={pix_fmt}")
    # For modest frame skips, a single sequential decode is cheaper than
    # starting FFmpeg once per sample. Sparse seeking is reserved for long
    # inputs where decoding the discarded frames dominates the work.
    skip_ratio = float(info.fps / rate) if rate > 0 else 1.0
    if skip_ratio < 32 or sample_count <= 2:
        if progress:
            progress(0, f"Decoding {info.path.name} to collect up to {sample_count} samples…")
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(info.path),
              "-map", "0:v:0", "-vf", vf, "-frames:v", str(max_frames), "-an",
              "-f", "rawvideo", "-pix_fmt", pix_fmt, str(output_path)])
        return

    width, height = target.width, target.height
    sample_bytes = 1 if target.bit_depth <= 8 else 2
    chroma_width = width >> target.subsampling_x
    chroma_height = height >> target.subsampling_y
    frame_bytes = (width * height + 2 * chroma_width * chroma_height) * sample_bytes
    vf = f"scale={width}:{height}:flags=lanczos,format={pix_fmt}"

    # A low-rate fps filter still decodes every frame in a long input. Seek to
    # each temporal sample instead, so only the GOP around each requested
    # timestamp needs to be decoded.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as raw_output:
        for index in range(sample_count):
            if progress and (index == 0 or (index + 1) % 5 == 0 or index + 1 == sample_count):
                progress(0, f"Seeking sample {index + 1}/{sample_count} from {info.path.name}…")
            timestamp = first_sample_time + index / float(rate)
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                       "-ss", f"{timestamp:.6f}", "-i", str(info.path),
                       "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
                       "-vf", vf, "-pix_fmt", pix_fmt, "-f", "rawvideo", "pipe:1"]
            try:
                result = subprocess.run(command, check=True, capture_output=True,
                                        creationflags=_NO_WINDOW)
            except FileNotFoundError as exc:
                raise RuntimeError(f"Could not find executable: {ffmpeg}") from exc
            except subprocess.CalledProcessError as exc:
                detail = exc.stderr.decode("utf-8", "replace").strip()
                raise RuntimeError(detail[-4000:] or f"FFmpeg failed to extract sample {index + 1}") from exc
            if len(result.stdout) != frame_bytes:
                break
            raw_output.write(result.stdout)


def run_aom_analysis(
    ffmpeg: str,
    noise_model: str,
    source: VideoInfo,
    encoded: VideoInfo,
    source_offset: int,
    encoded_offset: int,
    max_frames: int,
    raw_source: Path,
    raw_encoded: Path,
    table_path: Path,
    progress: Callable[[int, str], None] | None = None,
) -> tuple[int, Fraction]:
    """Extract matched planar frames and run AOM's official noise_model tool."""
    if encoded.width % 2 or encoded.height % 2:
        raise RuntimeError("AOM noise_model requires even width and height")

    rate, stride = _analysis_rate(encoded, max_frames)
    limit = max(2, max_frames)
    if progress:
        progress(0, f"Sampling source video (up to {limit} frames)…")
    _extract_raw(ffmpeg, source, encoded, raw_source, source_offset, limit, rate,
                 progress=progress)
    if progress:
        progress(0, f"Sampling encoded video (up to {limit} frames)…")
    _extract_raw(ffmpeg, encoded, encoded, raw_encoded, encoded_offset, limit, rate,
                 progress=progress)

    width, height = encoded.width, encoded.height
    sample_bytes = 1 if encoded.bit_depth == 8 else 2
    cw = width >> encoded.subsampling_x
    ch = height >> encoded.subsampling_y
    frame_bytes = (width * height + 2 * cw * ch) * sample_bytes
    n_frames = min(raw_source.stat().st_size, raw_encoded.stat().st_size) // frame_bytes
    if n_frames < 2:
        raise RuntimeError("Fewer than two matched frames were extracted; check duration and frame offsets")
    usable_size = n_frames * frame_bytes
    for raw_path in (raw_source, raw_encoded):
        with raw_path.open("r+b") as raw_file:
            raw_file.truncate(usable_size)

    if progress:
        progress(0, f"Estimating grain from {n_frames} matched frame pairs…")

    chroma_switch = "--i420" if (encoded.subsampling_x, encoded.subsampling_y) == (1, 1) else \
                    "--i422" if (encoded.subsampling_x, encoded.subsampling_y) == (1, 0) else "--i444"
    _run([noise_model, f"--width={width}", f"--height={height}",
          f"--fps={rate.numerator}/{rate.denominator}",
          f"--bit-depth={encoded.bit_depth}", chroma_switch,
          f"--input={raw_source}", f"--input-denoised={raw_encoded}",
          f"--output-grain-table={table_path}"])
    if not table_path.is_file():
        raise RuntimeError("noise_model finished without creating the grain table")
    if progress:
        progress(0, "Grain estimate complete; loading the parameter table…")
    return n_frames, rate


def extract_preview_png(ffmpeg: str, path: str | Path, seconds: float,
                        width: int = 960) -> bytes:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, seconds):.6f}",
         "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={width}:-2",
         "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True, timeout=60, creationflags=_NO_WINDOW)
    if result.returncode or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(detail or f"Could not extract preview from {Path(path).name}")
    return result.stdout


def render_vfgs_preview(ffmpeg: str, vfgs: str, encoded: VideoInfo,
                         seconds: float, params,
                         strength_percent: int, seed: int,
                         grain_scale_delta: int, overlap: bool,
                         scratch_dir: Path | None = None,
                         progress: Callable[[int, str], None] | None = None) -> bytes:
    """Render one decoded frame with InterDigital's optional VFGS model."""
    from copy import deepcopy
    from .grain import serialize_aom_single_entry

    preview_params = deepcopy(params)
    preview_params.grain_scale_shift = max(0, min(3, preview_params.grain_scale_shift + grain_scale_delta))
    preview_params.overlap_flag = 1 if overlap else 0
    cfg_text = serialize_aom_single_entry(preview_params)
    pix_fmt = analysis_pixel_format(encoded)
    fmt = "420" if encoded.subsampling_x == 1 and encoded.subsampling_y == 1 else \
          "422" if encoded.subsampling_x == 1 else "444"
    safe_seed = (int(seed) & 0x7FFFFFFF) or 1
    with tempfile.TemporaryDirectory(prefix="afgs1-vfgs-", dir=scratch_dir) as folder:
        folder = Path(folder)
        input_yuv = folder / "input.yuv"
        output_yuv = folder / "grain.yuv"
        table_path = folder / "preview.tbl"
        table_path.write_text(cfg_text, encoding="ascii")
        if progress:
            progress(0, f"Decoding the selected {seconds:.2f} s frame for VFGS…")
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, seconds):.6f}",
              "-i", str(encoded.path), "-map", "0:v:0", "-frames:v", "1",
              "-vf", f"scale={encoded.width}:{encoded.height}:flags=lanczos,format={pix_fmt}",
              "-pix_fmt", pix_fmt, "-f", "rawvideo", str(input_yuv)])
        if progress:
            progress(0, "Synthesizing grain with VFGS…")
        _run([vfgs, "--width", str(encoded.width), "--height", str(encoded.height),
              "--bitdepth", str(encoded.bit_depth), "--format", fmt,
              "--frames", "1", "--seek", "0", "--seed", str(safe_seed),
              "--cfg", str(table_path), "--gain", str(max(0, strength_percent)),
              str(input_yuv), str(output_yuv)])
        if progress:
            progress(0, "Preparing the synthesized frame for display…")
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
             "-pix_fmt", pix_fmt, "-video_size", f"{encoded.width}x{encoded.height}",
             "-i", str(output_yuv), "-frames:v", "1", "-vf", "scale=960:-2",
             "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        if result.returncode or not result.stdout:
            detail = result.stderr.decode("utf-8", "replace")[-1500:]
            raise RuntimeError(detail or "Could not convert the VFGS preview to PNG")
        return result.stdout


def estimate_offset_frames(ffmpeg: str, source: VideoInfo, encoded: VideoInfo,
                           search_seconds: float = 2.0,
                           preview_frames: int = 180,
                           scratch_dir: Path | None = None) -> int:
    """Estimate source-to-encode offset from low-resolution grayscale frame similarity."""
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Automatic alignment needs NumPy. Install the listed requirements.") from exc
    import tempfile

    # Keep the probe at the encoded frame rate so the resulting offset has
    # single-frame precision. Only the first few seconds are decoded.
    rate = encoded.fps
    with tempfile.TemporaryDirectory(prefix="afgs1-align-", dir=scratch_dir) as folder:
        raw_a = Path(folder) / "source.gray"
        raw_b = Path(folder) / "encoded.gray"

        def extract(info: VideoInfo, output: Path) -> None:
            vf = (f"scale=64:36:flags=area,format=gray,fps={rate.numerator}/{rate.denominator}")
            _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(info.path),
                  "-map", "0:v:0", "-vf", vf, "-frames:v", str(preview_frames),
                  "-f", "rawvideo", "-pix_fmt", "gray", str(output)])

        extract(source, raw_a)
        extract(encoded, raw_b)
        frame_pixels = 64 * 36
        frames_a = np.frombuffer(raw_a.read_bytes(), dtype=np.uint8)
        frames_b = np.frombuffer(raw_b.read_bytes(), dtype=np.uint8)
        count_a, count_b = len(frames_a) // frame_pixels, len(frames_b) // frame_pixels
        count = min(count_a, count_b, preview_frames)
        if count < 4:
            raise RuntimeError("Not enough frames for automatic alignment")
        a = frames_a[:count * frame_pixels].reshape(count, 36, 64).astype(np.int16)[:, ::2, ::2]
        b = frames_b[:count * frame_pixels].reshape(count, 36, 64).astype(np.int16)[:, ::2, ::2]
        max_lag = max(1, round(float(rate) * search_seconds))
        stride_t = max(1, len(a) // 48)
        best_lag, best_score = 0, float("inf")
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                left, right = a[:len(a) - lag or None], b[lag:]
            else:
                left, right = a[-lag:], b[:len(b) + lag]
            n = min(len(left), len(right))
            if n < 3:
                continue
            left, right = left[:n:stride_t], right[:n:stride_t]
            # Remove each frame's mean to reduce sensitivity to range/color shifts.
            left = left - left.mean(axis=(1, 2), keepdims=True)
            right = right - right.mean(axis=(1, 2), keepdims=True)
            score = float(np.mean(np.abs(left - right)))
            if score < best_score:
                best_lag, best_score = lag, score
    # The probe runs at the encoded CFR, so each lag is one encoded frame.
    return -round(best_lag * float(encoded.fps / rate))
