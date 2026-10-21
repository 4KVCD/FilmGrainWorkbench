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
import os
import struct
import subprocess
import tempfile
import threading
import time

import numpy as np

from . import synth
from .alignment import check_alignment
from .frames import Crop, Frame, Geometry, filter_chain, read_frames, seek_args
from .grain_params import GrainParams, TableEntry, bulk_objects, format_grain_table, parse_grain_table
from .probe import VideoInfo
from .tools import Cancelled, CancelToken, ToolError, Tools, cpu_count, run

CHECKPOINT_EVERY = 1000      # frames between saved model states (~1 min of 4K work)
PREFLIGHT_PLACES = 8         # alignment checks spread over the film before starting
PREFLIGHT_FRAMES = 7         # consecutive frames per check (detects offsets up to +/-3)
PLOT_SAMPLES = 2000          # points on the grain-over-time strip
CPU_SECONDS_PER_4K_FRAME = 1.85   # analyser + decoding, one thread (The Beekeeper, see estimate_seconds)
THREAD_EFFICIENCY = 0.85          # what each extra thread adds, measured on 24 threads
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Segment:
    start: int                   # first encoded frame (inclusive)
    end: int                     # last encoded frame (exclusive)
    params: GrainParams


@dataclass
class TimelineModel:
    """Grain parameters that change over the video, one segment at a time."""
    segments: list[Segment]
    bit_depth: int
    subsampling_x: int
    subsampling_y: int
    fps: Fraction
    frames: int
    seconds: float = 0.0
    kind: str = "every-frame"
    sigma_frames: list[int] = field(default_factory=list)    # grain-over-time samples
    sigma_values: list[float] = field(default_factory=list)  # luma sigma at mid-grey, sample units
    alignment: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)  # "warnings" from the run

    def __post_init__(self) -> None:
        self.segments.sort(key=lambda s: s.start)
        self._starts = [s.start for s in self.segments]

    def segment_at(self, index: int) -> Segment:
        i = max(0, bisect_right(self._starts, index) - 1)
        return self.segments[i]

    def to_json(self) -> str:
        table = format_grain_table([TableEntry(s.start, s.end, s.params) for s in self.segments])
        return json.dumps({
            "version": 3, "kind": "timeline", "mode": self.kind, "bit_depth": self.bit_depth,
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
        # Version 2 models came from reads that could land on the wrong frame.
        if d.get("kind") != "timeline" or d.get("version") != 3:
            raise ValueError("not a grain model this version can read")
        with bulk_objects():
            segments = [Segment(e.start, e.end, e.params) for e in parse_grain_table(d["table_in_frames"])]
        return cls(segments, d["bit_depth"], *d["subsampling"], Fraction(*d["fps"]), d["frames"],
                   d.get("seconds", 0.0), d.get("mode", "every-frame"), d.get("sigma_frames", []),
                   d.get("sigma_values", []), d.get("alignment", []), d.get("notes", {}))

    def grain_table(self) -> list[TableEntry]:
        """Entries with real timestamps (100 ns units) for aomenc / SVT-AV1.

        Like the written MKV, the whole video is covered: frames before the
        first analysed one (a cut file's undecodable start) take the first
        segment, frames after the last one take the last. A table with gaps
        would give those frames no grain at all.
        """
        to_ts = lambda frame: int(frame * 10_000_000 * self.fps.denominator // self.fps.numerator)
        last = len(self.segments) - 1
        return [TableEntry(to_ts(0 if i == 0 else s.start), to_ts(max(s.end, self.frames) if i == last else s.end),
                           s.params) for i, s in enumerate(self.segments)]


# --------------------------------------------------------------------------- helpers

def analyzer_path() -> Path:
    return Path(__file__).resolve().parents[1] / "bin" / "afgs1_grain_analyzer.exe"


def default_threads() -> int:
    return cpu_count()


def estimate_seconds(encoded: VideoInfo, threads: int, source: VideoInfo | None = None) -> float:
    """Rough wall time: ~11 fps on 24 threads for 4K, decoding included.

    Measured on The Beekeeper: the 4K cut ran at ~11 fps, and the whole film
    with a 3840x1608 encode at 11.4 fps; decoding the full 4K source costs as
    much as the smaller encode saves, so the larger of the two pictures sets
    the pace.
    """
    pixels = max(encoded.width * encoded.height, source.width * source.height if source else 0)
    scale = pixels / (3840 * 2160)
    cpu_per_frame = CPU_SECONDS_PER_4K_FRAME * scale
    return encoded.frame_count * cpu_per_frame / max(1, min(threads, cpu_count()) * THREAD_EFFICIENCY)


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
    parts += [str(offset), str(crop), "analyzer-v2"]
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

def first_common_frame(tools: Tools, source: VideoInfo, encoded: VideoInfo, offset: int,
                       crop: Crop | None, first: int, cancel: CancelToken) -> int:
    """First encoded frame at or after ``first`` that both files can decode.

    A file cut from a longer one starts with leading pictures that reference
    frames before the cut; ffmpeg drops them (a cut encode's first 15).
    """
    geometry = Geometry.of(encoded)
    enc = read_frames(tools.ffmpeg, encoded, first, 1, cancel)[0].index
    src = read_frames(tools.ffmpeg, source, first + offset, 1, cancel, geometry, crop)[0].index - offset
    if min(enc, src) < first:
        raise RuntimeError(f"seeking to frame {first} returned frame {min(enc, src)}")
    return max(enc, src)


def preflight_alignment(tools: Tools, source: VideoInfo, encoded: VideoInfo, offset: int,
                        crop: Crop | None, cancel: CancelToken, progress,
                        first: int | None = None) -> list[str]:
    """Check frame-accurate pairing at several places before hours of work.

    Frames are paired by their own timestamps, like the every-frame pipeline,
    and the places stay clear of the ends, where cut files have frames that do
    not decode. Two places at a time: each 4K check holds two decoders (~1 GB).
    """
    lo, last = frame_range(encoded, source, offset)
    first = lo if first is None else first
    geometry = Geometry.of(encoded)
    margin = min(PREFLIGHT_FRAMES, max(0, (last - first) // 4))
    places = np.linspace(first + margin, max(first, last - PREFLIGHT_FRAMES - margin),
                         PREFLIGHT_PLACES).astype(int)
    peak = float((1 << geometry.bit_depth) - 1)

    def check(place: int) -> str:
        s = read_frames(tools.ffmpeg, source, int(place) + offset, PREFLIGHT_FRAMES, cancel, geometry, crop)
        e = read_frames(tools.ffmpeg, encoded, int(place), PREFLIGHT_FRAMES, cancel)
        by_index = {f.index - offset: f for f in s}
        pairs = [(by_index[f.index], f) for f in e if f.index in by_index]
        if len(pairs) < 3:
            return "unclear"
        src = np.stack([a.y[::4, ::4].astype(np.float32) for a, _ in pairs])
        enc = np.stack([b.y[::4, ::4].astype(np.float32) for _, b in pairs])
        return check_alignment(src, enc, peak)[1]

    verdicts: list[str] = []
    with ThreadPoolExecutor(2) as pool:
        for i, verdict in enumerate(pool.map(check, places)):
            verdicts.append(verdict)
            progress(-1, f"Checking that source and encode frames line up ({i + 1} of {len(places)})…")
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

    # Each frame's timestamp becomes its encoded-frame number, so vstack pairs
    # frames by time, not by arrival: a frame one decoder drops (or a file's
    # undecodable leading pictures) costs one repeated frame instead of
    # shifting every later pair. The analyzer numbers frames from ``start``.
    def number(info: VideoInfo, shift: int) -> str:
        return (f"settb={tb},setpts='round((T-{info.first_pts:.6f})*{fps.numerator}/{fps.denominator})"
                f"{-shift:+d}'")
    graph = (f"[0:{source.stream_index}]{filter_chain(source, g, crop)},{number(source, offset)}[s];"
             f"[1:{encoded.stream_index}]{filter_chain(encoded, g, None)},{number(encoded, 0)}[e];"
             f"[s][e]vstack=inputs=2:shortest=1[o]")
    ffmpeg = [tools.ffmpeg, "-v", "error", "-nostdin"]
    if source_hwaccel:
        ffmpeg += ["-hwaccel", "auto"]
    ffmpeg += ["-copyts", *seek_args(source, start + offset), "-i", str(source.path),
               *seek_args(encoded, start), "-i", str(encoded.path),
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
    warnings: list[str] = []
    seconds = 0.0                        # how long the analysis itself took (not reading it back)
    resumed = False
    if done_marker.exists() and table.exists():
        marker = json.loads(done_marker.read_text())
        alignment, warnings = marker.get("alignment", []), marker.get("warnings", [])
        seconds, resumed = float(marker.get("seconds", 0.0)), bool(marker.get("resumed", False))
    else:
        progress(-1, "Checking that source and encode frames line up…")
        first = first_common_frame(tools, source, encoded, offset, crop, first, cancel)
        alignment = preflight_alignment(tools, source, encoded, offset, crop, cancel, progress, first)
        resume = resume_at is not None and first < resume_at < last
        start = resume_at if resume else first
        count = last - start
        ffmpeg_cmd, analyzer_cmd = _pipeline_commands(
            tools, source, encoded, offset, crop, start, count, source_hwaccel, analyzer,
            threads, table, checkpoint, resume)
        decoder = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        cancel.track(decoder)
        worker = subprocess.Popen(analyzer_cmd, stdin=decoder.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
        decoder.stdout.close()                      # the analyzer owns the pipe now
        cancel.track(worker)
        dec_err, ana_err = deque(maxlen=20), deque(maxlen=20)
        threading.Thread(target=_drain, args=(decoder.stderr, dec_err), daemon=True).start()
        threading.Thread(target=_drain, args=(worker.stderr, ana_err), daemon=True).start()
        total = last - first
        clock, base = time.perf_counter(), start
        done = start
        try:
            for line in iter(worker.stdout.readline, b""):
                if line.startswith(b"D "):
                    done = int(line.split()[1])
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
            cancel.untrack(worker)
            cancel.untrack(decoder)
            for proc in (worker, decoder):
                if proc.poll() is None:
                    proc.kill()
        cancel.check()
        if worker.returncode != 0:
            raise ToolError("grain analyzer failed: " + (" / ".join(list(ana_err)[-3:]) or
                                                        f"exit code {worker.returncode}"))
        if decoder.returncode != 0:
            raise ToolError("ffmpeg failed while decoding: " + " / ".join(list(dec_err)[-3:]))
        if last - done > max(2, int(float(encoded.fps))):
            # The encode ends earlier than its header says: the last frames keep
            # the final segment's grain. Anything shorter is rounding of the length.
            warnings.append(f"Decoding stopped at frame {done:,} of {last:,}; the frames after it "
                            "use the last measured grain.")
        if first > 0:
            warnings.append(f"Frames before {first:,} could not be decoded in both files; they use "
                            "the first measured grain.")
        seconds = time.perf_counter() - started       # this run only, if it resumed an earlier one
        resumed = resume
        done_marker.write_text(json.dumps({"alignment": alignment, "warnings": warnings, "seconds": seconds,
                                           "resumed": resume}))
        checkpoint.unlink(missing_ok=True)

    progress(0.99, "Summarising the grain over time…")
    with bulk_objects():
        segments = [Segment(e.start, e.end, e.params)
                    for e in parse_grain_table(table.read_text(encoding="ascii"))]
    geometry = Geometry.of(encoded)
    model = TimelineModel(segments, geometry.bit_depth, geometry.subsampling_x, geometry.subsampling_y,
                          encoded.fps, encoded.frame_count, seconds,
                          "every-frame", alignment=alignment, notes={"warnings": warnings, "resumed": resumed})
    sample_sigmas(model, cancel)
    return model


def estimate_frame(src: Frame, enc: Frame, cancel: CancelToken | None = None,
                   analyzer: Path | None = None) -> GrainParams:
    """Grain parameters of one frame: libaom's noise model started fresh on it.

    This is what the encoder derives for the first frame of a shot (~0.7 s
    for a 4K frame). The every-frame analysis carries the model across
    frames, so its parameters for the same frame can differ a little.
    Returns apply_grain=False when the model finds no usable flat areas.
    """
    g = enc.geometry
    dtype = "<u2" if g.bit_depth > 8 else "u1"
    data = b"".join(np.ascontiguousarray(p, dtype=dtype).tobytes()
                    for p in (src.y, enc.y, src.u, enc.u, src.v, enc.v))
    chroma = {(1, 1): "420", (1, 0): "422", (0, 0): "444"}[(g.subsampling_x, g.subsampling_y)]
    fd, name = tempfile.mkstemp(suffix=".tbl", prefix="afgs1wb-frame-")
    os.close(fd)
    table = Path(name)
    try:
        run([str(analyzer or analyzer_path()), "--width", str(g.width), "--height", str(g.height),
             "--bit-depth", str(g.bit_depth), "--chroma", chroma, "--threads", "1",
             "--output", str(table)], cancel, stdin=data)
        entries = parse_grain_table(table.read_text(encoding="ascii"))
    finally:
        table.unlink(missing_ok=True)
    return entries[0].params if entries else GrainParams(apply_grain=False)
