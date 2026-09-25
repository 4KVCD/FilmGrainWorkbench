"""Every-frame grain analysis and the time-varying result.

libaom's encoder (aomenc --denoise-noise-level) updates its noise model on
every frame and derives that frame's film grain parameters from the running
model, restarting the model when the noise changes. This module does the same
for a (source, encode) pair:

    ffmpeg (one process, both videos decoded once, in order, stacked)
        -> afgs1_grain_analyzer (libaom's noise model, per-frame work on N threads)
        -> table of parameter segments (frame numbers)

The analyzer saves the running model every CHECKPOINT_EVERY frames, so an
interrupted run resumes exactly where it stopped.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import struct
import subprocess
import threading
import time

import numpy as np

from . import synth
from .analysis import check_alignment
from .frames import Crop, Geometry, _filters, luma_planes, read_frames
from .grain_params import GrainParams, TableEntry, format_grain_table, parse_grain_table
from .probe import VideoInfo
from .tools import Cancelled, CancelToken, ToolError, Tools, cpu_count

CHECKPOINT_EVERY = 1000      # frames between saved model states (~1 min of 4K work)
PREFLIGHT_PLACES = 8         # alignment checks spread over the film before starting
PREFLIGHT_FRAMES = 7         # consecutive frames per check (detects offsets up to +/-3)
PLOT_SAMPLES = 2000          # points on the grain-over-time strip
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Segment:
    start: int                   # first encoded frame (inclusive)
    end: int                     # last encoded frame (exclusive)
    params: GrainParams


@dataclass
class TimelineModel:
    """Grain parameters that change over the video, one segment at a time.

    A Quick-sample analysis is the same thing with a single segment.
    """
    segments: list[Segment]
    bit_depth: int
    subsampling_x: int
    subsampling_y: int
    fps: Fraction
    frames: int
    seconds: float = 0.0
    kind: str = "every-frame"                 # or "quick"
    sigma_frames: list[int] = field(default_factory=list)    # grain-over-time samples
    sigma_values: list[float] = field(default_factory=list)  # luma sigma at mid-grey, sample units
    alignment: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)  # mode-specific extras (quick runs etc.)

    def __post_init__(self) -> None:
        self.segments.sort(key=lambda s: s.start)
        self._starts = [s.start for s in self.segments]

    def segment_at(self, index: int) -> Segment:
        i = max(0, bisect_right(self._starts, index) - 1)
        return self.segments[i]

    def to_json(self) -> str:
        table = format_grain_table([TableEntry(s.start, s.end, s.params) for s in self.segments])
        return json.dumps({
            "version": 2, "kind": "timeline", "mode": self.kind, "bit_depth": self.bit_depth,
            "subsampling": [self.subsampling_x, self.subsampling_y],
            "fps": [self.fps.numerator, self.fps.denominator], "frames": self.frames,
            "seconds": round(self.seconds, 1), "alignment": self.alignment,
            "sigma_frames": self.sigma_frames,
            "sigma_values": [round(v, 4) for v in self.sigma_values],
            "notes": self.notes, "table_in_frames": table,
        })

    @classmethod
    def from_json(cls, text: str) -> "TimelineModel":
        d = json.loads(text)
        if d.get("kind") != "timeline" or d.get("version") != 2:
            raise ValueError("not a grain model this version can read")
        segments = [Segment(e.start, e.end, e.params) for e in parse_grain_table(d["table_in_frames"])]
        return cls(segments, d["bit_depth"], *d["subsampling"], Fraction(*d["fps"]), d["frames"],
                   d.get("seconds", 0.0), d.get("mode", "every-frame"), d.get("sigma_frames", []),
                   d.get("sigma_values", []), d.get("alignment", []), d.get("notes", {}))

    def grain_table(self) -> list[TableEntry]:
        """Entries with real timestamps (100 ns units) for aomenc / SVT-AV1."""
        to_ts = lambda frame: int(frame * 10_000_000 * self.fps.denominator // self.fps.numerator)
        return [TableEntry(to_ts(s.start), to_ts(s.end), s.params) for s in self.segments]


# --------------------------------------------------------------------------- helpers

def analyzer_path() -> Path:
    return Path(__file__).resolve().parents[1] / "bin" / "afgs1_grain_analyzer.exe"


def default_threads() -> int:
    return cpu_count()


def estimate_seconds(encoded: VideoInfo, threads: int) -> float:
    """Rough wall time: ~1 CPU-second per 4K frame of noise-model work, plus decoding."""
    scale = encoded.width * encoded.height / (3840 * 2160)
    cpu_per_frame = 1.15 * scale
    return encoded.frame_count * cpu_per_frame / max(1, min(threads, cpu_count()) * 0.85)


def frame_range(encoded: VideoInfo, source: VideoInfo, offset: int) -> tuple[int, int]:
    first = max(0, -offset)
    last = min(encoded.frame_count, source.frame_count - offset)
    if last - first < 2:
        raise ValueError("the source and encode do not overlap")
    return first, last


def cache_key(source: VideoInfo, encoded: VideoInfo, offset: int, crop: Crop | None) -> str:
    parts = []
    for info in (source, encoded):
        st = info.path.stat()
        parts.append(f"{info.path}|{st.st_size}|{int(st.st_mtime)}")
    parts += [str(offset), str(crop), "analyzer-v1"]
    return hashlib.sha1("||".join(parts).encode()).hexdigest()[:16]


def checkpoint_frame(path: Path) -> int | None:
    """Next frame recorded in an analyzer checkpoint (magic, version, then int64)."""
    try:
        head = path.read_bytes()[:16]
        magic, version, frame = struct.unpack("<IIq", head)
    except (OSError, struct.error):
        return None
    return frame if magic == 0x31534741 and version == 1 else None


def _segment_sigma(p: GrainParams, bit_depth: int) -> float:
    if not p.y_points or not p.apply_grain:
        return 0.0
    lut = synth.scaling_lut(p.y_points)
    return float(lut[128]) / (1 << p.scaling_shift) * synth.luma_template_std(p, bit_depth, seeds=(1,))


def _sigmas_batch(args: tuple[list[GrainParams], int]) -> list[float]:
    params, bit_depth = args
    return [_segment_sigma(p, bit_depth) for p in params]


def sample_sigmas(model: TimelineModel, cancel: CancelToken) -> None:
    """Fill sigma_frames/values with up to PLOT_SAMPLES points (~18 ms each, in processes)."""
    if not model.segments:
        return
    wanted = np.unique(np.linspace(0, model.frames - 1, min(PLOT_SAMPLES, model.frames)).astype(int))
    unique: dict[int, int] = {}
    frames, keys = [], []
    for f in wanted:
        seg = model.segment_at(int(f))
        unique.setdefault(seg.start, len(unique))
        frames.append(int(f))
        keys.append(seg.start)
    params = [model.segment_at(start).params for start in unique]
    results: list[float] = []
    batches = [params[i:i + 50] for i in range(0, len(params), 50)]
    try:
        if len(batches) <= 2:
            raise OSError("small job")
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max(2, cpu_count() - 2)) as pool:
            for out in pool.map(_sigmas_batch, [(b, model.bit_depth) for b in batches]):
                cancel.check()
                results += out
    except (OSError, Exception) as exc:           # BrokenProcessPool, spawn problems, small jobs
        if isinstance(exc, Cancelled):
            raise
        results = []
        for i, p in enumerate(params):
            if i % 50 == 0:
                cancel.check()
            results.append(_segment_sigma(p, model.bit_depth))
    by_start = dict(zip(unique, results))
    model.sigma_frames = frames
    model.sigma_values = [by_start[k] for k in keys]


# --------------------------------------------------------------------------- preflight

def preflight_alignment(tools: Tools, source: VideoInfo, encoded: VideoInfo, offset: int,
                        crop: Crop | None, cancel: CancelToken, progress) -> list[str]:
    """Check frame-accurate pairing at several places before hours of work."""
    first, last = frame_range(encoded, source, offset)
    geometry = Geometry.of(encoded)
    places = np.linspace(first, max(first, last - PREFLIGHT_FRAMES), PREFLIGHT_PLACES).astype(int)
    peak = float((1 << geometry.bit_depth) - 1)
    ny = geometry.width * geometry.height

    def luma(frames) -> np.ndarray:
        return np.stack([f.y[::4, ::4].astype(np.float32) for f in frames])

    def check(place: int) -> str:
        s = read_frames(tools.ffmpeg, source, int(place) + offset, PREFLIGHT_FRAMES, cancel, geometry, crop)
        e = read_frames(tools.ffmpeg, encoded, int(place), PREFLIGHT_FRAMES, cancel)
        n = min(len(s), len(e))
        return check_alignment(luma(s[:n]), luma(e[:n]), peak)[1] if n >= 3 else "unclear"

    verdicts: list[str] = []
    with ThreadPoolExecutor(4) as pool:
        for i, verdict in enumerate(pool.map(check, places)):
            verdicts.append(verdict)
            progress(-1, f"Checking that source and encode frames line up ({i + 1} of {len(places)})…")
    del ny
    if verdicts.count("shifted") * 2 > len(verdicts) or \
            (verdicts.count("shifted") >= 2 and verdicts.count("ok") == 0):
        raise RuntimeError(
            f"Source and encode frames do not line up at {verdicts.count('shifted')} of {len(verdicts)} "
            "places checked (they match neighbouring frames better). Use Detect to set the frame "
            "offset, then start again.")
    return verdicts


# --------------------------------------------------------------------------- run

def _pipeline_commands(tools: Tools, source: VideoInfo, encoded: VideoInfo, offset: int,
                       crop: Crop | None, start: int, count: int, source_hwaccel: bool,
                       analyzer: Path, threads: int, table: Path, checkpoint: Path,
                       resume: bool) -> tuple[list[str], list[str]]:
    g = Geometry.of(encoded)
    fps = encoded.fps
    tb = f"{fps.denominator}/{fps.numerator}"
    src_seek = source.first_pts + (start + offset - 0.5) / float(source.fps)
    enc_seek = encoded.first_pts + (start - 0.5) / float(encoded.fps)
    graph = (f"[0:{source.stream_index}]{_filters(source, g, crop)},settb={tb},setpts=N[s];"
             f"[1:{encoded.stream_index}]{_filters(encoded, g, None)},settb={tb},setpts=N[e];"
             f"[s][e]vstack=inputs=2:shortest=1[o]")
    ffmpeg = [tools.ffmpeg, "-v", "error", "-nostdin"]
    if source_hwaccel:
        ffmpeg += ["-hwaccel", "auto"]
    ffmpeg += ["-seek_timestamp", "1", "-ss", f"{max(0.0, src_seek):.6f}", "-i", str(source.path),
               "-seek_timestamp", "1", "-ss", f"{max(0.0, enc_seek):.6f}", "-i", str(encoded.path),
               "-filter_complex", graph, "-map", "[o]", "-frames:v", str(count),
               "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", g.pix_fmt, "pipe:1"]
    chroma = {(1, 1): "420", (1, 0): "422", (0, 0): "444"}[(g.subsampling_x, g.subsampling_y)]
    analyzer_cmd = [str(analyzer), "--width", str(g.width), "--height", str(g.height),
                    "--bit-depth", str(g.bit_depth), "--chroma", chroma, "--threads", str(threads),
                    "--start-frame", str(start), "--output", str(table),
                    "--checkpoint", str(checkpoint), "--checkpoint-every", str(CHECKPOINT_EVERY)]
    if resume:
        analyzer_cmd += ["--resume", str(checkpoint)]
    return ffmpeg, analyzer_cmd


def _drain(stream, sink: deque) -> None:
    for line in iter(stream.readline, b""):
        sink.append(line.decode("utf-8", "replace").rstrip())


def analyze_every_frame(tools: Tools, source: VideoInfo, encoded: VideoInfo, offset: int,
                        crop: Crop | None, work_dir: Path, progress, cancel: CancelToken,
                        threads: int | None = None, source_hwaccel: bool = True,
                        analyzer: Path | None = None) -> TimelineModel:
    started = time.perf_counter()
    analyzer = Path(analyzer or analyzer_path())
    if not analyzer.is_file():
        raise ToolError(f"The grain analyzer is not built ({analyzer}). Run analyzer/build.py.")
    threads = threads or default_threads()
    first, last = frame_range(encoded, source, offset)
    work_dir.mkdir(parents=True, exist_ok=True)
    table, checkpoint = work_dir / "table.tbl", work_dir / "checkpoint.bin"
    done_marker = work_dir / "done.json"

    resume_at = checkpoint_frame(checkpoint) if checkpoint.exists() and table.exists() else None
    if done_marker.exists() and table.exists():
        alignment = json.loads(done_marker.read_text()).get("alignment", [])
    else:
        progress(-1, "Checking that source and encode frames line up…")
        alignment = preflight_alignment(tools, source, encoded, offset, crop, cancel, progress)
        start = resume_at if resume_at is not None and first <= resume_at < last else first
        resume = start != first
        count = last - start
        ffmpeg_cmd, analyzer_cmd = _pipeline_commands(
            tools, source, encoded, offset, crop, start, count, source_hwaccel, analyzer,
            threads, table, checkpoint, resume)
        decoder = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        cancel._track(decoder)
        worker = subprocess.Popen(analyzer_cmd, stdin=decoder.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
        decoder.stdout.close()                      # the analyzer owns the pipe now
        cancel._track(worker)
        dec_err, ana_err = deque(maxlen=20), deque(maxlen=20)
        threading.Thread(target=_drain, args=(decoder.stderr, dec_err), daemon=True).start()
        threading.Thread(target=_drain, args=(worker.stderr, ana_err), daemon=True).start()
        total = last - first
        clock, base = time.perf_counter(), start
        done = start
        try:
            for line in iter(worker.stdout.readline, b""):
                if not line.startswith(b"P "):
                    continue
                done = int(line.split()[1])
                elapsed = time.perf_counter() - clock
                rate = (done - base) / elapsed if elapsed > 3 else 0.0
                text = f"Analysed {done - first:,} of {total:,} frames"
                if rate:
                    left = (last - done) / rate
                    h, m = divmod(int(left // 60), 60)
                    text += f" ({rate:.1f} fps, about {h} h {m:02d} min left)" if h else \
                        f" ({rate:.1f} fps, about {max(1, m)} min left)"
                if resume:
                    text += " · resumed"
                progress((done - first) / total, text)
            worker.wait()
            decoder.wait()
        finally:
            cancel._untrack(worker)
            cancel._untrack(decoder)
            for proc in (worker, decoder):
                if proc.poll() is None:
                    proc.kill()
        cancel.check()
        if worker.returncode != 0:
            raise ToolError("grain analyzer failed: " + (" / ".join(list(ana_err)[-3:]) or
                                                        f"exit code {worker.returncode}"))
        if decoder.returncode != 0:
            raise ToolError("ffmpeg failed while decoding: " + " / ".join(list(dec_err)[-3:]))
        done_marker.write_text(json.dumps({"alignment": alignment}))
        checkpoint.unlink(missing_ok=True)

    progress(0.99, "Summarising the grain over time…")
    segments = [Segment(e.start, e.end, e.params)
                for e in parse_grain_table(table.read_text(encoding="ascii"))]
    geometry = Geometry.of(encoded)
    model = TimelineModel(segments, geometry.bit_depth, geometry.subsampling_x, geometry.subsampling_y,
                          encoded.fps, encoded.frame_count, time.perf_counter() - started,
                          "every-frame", alignment=alignment)
    sample_sigmas(model, cancel)
    return model


def quick_timeline(params: GrainParams, encoded: VideoInfo, seconds: float, notes: dict) -> TimelineModel:
    """A Quick-sample result as a one-segment timeline."""
    g = Geometry.of(encoded)
    model = TimelineModel([Segment(0, encoded.frame_count, params)], g.bit_depth, g.subsampling_x,
                          g.subsampling_y, encoded.fps, encoded.frame_count, seconds, "quick",
                          notes=notes)
    sigma = _segment_sigma(params, g.bit_depth)
    model.sigma_frames, model.sigma_values = [0, encoded.frame_count - 1], [sigma, sigma]
    return model
