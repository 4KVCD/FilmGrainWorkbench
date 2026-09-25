"""Every-frame grain analysis with AOM's noise_model, and the time-varying result.

Each frame of the encode is compared with the matching source frame by the
unmodified libaom ``noise_model`` program, as libaom's own encoder does with
--denoise-noise-level: the noise model accumulates frame by frame and a new
parameter segment starts whenever it detects a different kind of noise.

``noise_model`` only reads files and is single-threaded (~1 s per 4K frame),
so the video is cut into fixed chunks, each streamed from two ffmpeg decoders
through Windows named pipes into its own ``noise_model`` process, several at
once. Finished chunks are kept on disk so an interrupted run resumes.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import subprocess
import threading
import time

import numpy as np

from . import synth
from .analysis import check_alignment
from .frames import Crop, Geometry, _command
from .grain_params import GrainParams, TableEntry, format_grain_table, parse_grain_table
from .probe import VideoInfo
from .tools import Cancelled, CancelToken, ToolError, Tools, cpu_count
from .winpipe import NamedPipeWriter, PipeClosed

CHUNK_FRAMES = 3000          # ~2 minutes of film per noise_model process
SAMPLE_EVERY = 500           # alignment spot-check spacing (frames)
SAMPLE_FRAMES = 5            # consecutive frames per spot check (detects offsets up to +/-3)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Segment:
    start: int                   # first encoded frame (inclusive)
    end: int                     # last encoded frame (exclusive)
    params: GrainParams
    luma_mid_sigma: float = 0.0  # output grain sigma at mid-grey, sample units


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
    shifted_chunks: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.segments.sort(key=lambda s: s.start)
        self._starts = [s.start for s in self.segments]

    def segment_at(self, index: int) -> Segment:
        i = max(0, bisect_right(self._starts, index) - 1)
        return self.segments[i]

    def to_json(self) -> str:
        table = format_grain_table([TableEntry(s.start, s.end, s.params) for s in self.segments])
        return json.dumps({
            "version": 1, "kind": "timeline", "bit_depth": self.bit_depth,
            "subsampling": [self.subsampling_x, self.subsampling_y],
            "fps": [self.fps.numerator, self.fps.denominator], "frames": self.frames,
            "seconds": round(self.seconds, 1), "shifted_chunks": self.shifted_chunks,
            "table_in_frames": table,
            "luma_mid_sigma": [round(s.luma_mid_sigma, 4) for s in self.segments],
        })

    @classmethod
    def from_json(cls, text: str) -> "TimelineModel":
        d = json.loads(text)
        if d.get("kind") != "timeline" or d.get("version") != 1:
            raise ValueError("not an every-frame grain model")
        entries = parse_grain_table(d["table_in_frames"])
        segments = [Segment(e.start, e.end, e.params, sigma)
                    for e, sigma in zip(entries, d["luma_mid_sigma"])]
        return cls(segments, d["bit_depth"], *d["subsampling"], Fraction(*d["fps"]), d["frames"],
                   d.get("seconds", 0.0), d.get("shifted_chunks", []))

    def grain_table(self) -> list[TableEntry]:
        """Entries with real timestamps (100 ns units) for aomenc / SVT-AV1."""
        to_ts = lambda frame: int(frame * 10_000_000 * self.fps.denominator // self.fps.numerator)
        return [TableEntry(to_ts(s.start), to_ts(s.end), s.params) for s in self.segments]


# --------------------------------------------------------------------------- estimate

def estimate_seconds(encoded: VideoInfo, workers: int) -> float:
    """Wall-time estimate, from runs on a Core Ultra 9 285K (0.13 s/frame at 4K, 12 workers)."""
    per_frame_4k = 0.13 * 12 / max(1, workers) ** 0.85 * (12 ** 0.15)
    scale = encoded.width * encoded.height / (3840 * 2160)
    return encoded.frame_count * per_frame_4k * scale * (24 / cpu_count())


def default_workers() -> int:
    return max(2, min(12, cpu_count() // 2))


# --------------------------------------------------------------------------- chunk runner

def _drain(stream, sink: deque) -> None:
    for line in iter(stream.readline, b""):
        sink.append(line.decode("utf-8", "replace").rstrip())


class _ChunkRun:
    """One contiguous chunk: two ffmpeg decoders -> named pipes -> noise_model."""

    def __init__(self, tools: Tools, source: VideoInfo, encoded: VideoInfo, geometry: Geometry,
                 crop: Crop | None, offset: int, start: int, count: int, table: Path,
                 cancel: CancelToken, source_hwaccel: bool) -> None:
        self.tools, self.source, self.encoded = tools, source, encoded
        self.geometry, self.crop, self.offset = geometry, crop, offset
        self.start, self.count, self.table, self.cancel = start, count, table, cancel
        self.source_hwaccel = source_hwaccel
        self.frames_done = 0
        self.finished = False
        self.samples: dict[tuple[str, int], np.ndarray] = {}
        self.alignment: list[str] = []
        self._procs: list[subprocess.Popen] = []
        self._pipes: list[NamedPipeWriter] = []
        self._errors: list[str] = []
        self.delivered: dict[str, int] = {}
        self._stopping = False
        self._lock = threading.Lock()
        self.reported = False

    def _decoder(self, which: str) -> list[str]:
        info = self.source if which == "src" else self.encoded
        index = self.start + (self.offset if which == "src" else 0)
        cmd = _command(self.tools.ffmpeg, info, index, self.count, self.geometry,
                       self.crop if which == "src" else None, "pipe:1")
        extra = ["-threads", "2"]
        if which == "src" and self.source_hwaccel:
            extra = ["-hwaccel", "auto"] + extra
        return cmd[:1] + extra + cmd[1:]

    def _relay(self, which: str, pipe: NamedPipeWriter) -> None:
        try:
            pipe.wait_for_reader()
            proc = subprocess.Popen(self._decoder(which), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                    creationflags=_NO_WINDOW)
            self._procs.append(proc)
            self.cancel._track(proc)
            errors: deque = deque(maxlen=20)
            threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True).start()
            frame_bytes = self.geometry.frame_bytes
            ny = self.geometry.width * self.geometry.height
            dtype = np.uint16 if self.geometry.bit_depth > 8 else np.uint8
            k = 0
            while True:
                buf = proc.stdout.read(frame_bytes)
                if not buf:
                    break
                if len(buf) < frame_bytes:
                    break
                if k % SAMPLE_EVERY < SAMPLE_FRAMES:
                    y = np.frombuffer(buf, dtype=dtype, count=ny).reshape(
                        self.geometry.height, self.geometry.width)[::4, ::4].astype(np.float32)
                    self.samples[(which, k)] = y
                pipe.write(buf)
                k += 1
                self.delivered[which] = k
            proc.wait()
            self.cancel._untrack(proc)
            # A decoder we stopped during cleanup is not a failure; one that died
            # before delivering its frames is.
            if proc.returncode not in (0, None) and not self.cancel.cancelled                     and not (self._stopping and k >= self.count):
                self._errors.append(f"ffmpeg ({which}) failed after {k} of {self.count} frames: "
                                    + (" / ".join(list(errors)[-3:]) or f"exit code {proc.returncode}"))
        except PipeClosed:
            pass
        finally:
            self.delivered.setdefault(which, 0)
            pipe.close()

    def check_alignment(self, final: bool = False) -> str | None:
        """Verdict for the next complete spot-check sample, or None if not ready yet."""
        with self._lock:
            base = len(self.alignment) * SAMPLE_EVERY
            wanted = range(SAMPLE_FRAMES) if not final else [
                d for d in range(SAMPLE_FRAMES) if ("src", base + d) in self.samples]
            keys = [(w, base + d) for w in ("src", "enc") for d in wanted]
            if len(wanted) < 3 or not all(k in self.samples for k in keys):
                return None
            src = np.stack([self.samples.pop(("src", base + d)) for d in wanted])
            enc = np.stack([self.samples.pop(("enc", base + d)) for d in wanted])
            verdict = check_alignment(src, enc, float((1 << self.geometry.bit_depth) - 1))[1]
            self.alignment.append(verdict)
            return verdict

    def run(self) -> list[Segment]:
        g = self.geometry
        chroma = {(1, 1): "--i420", (1, 0): "--i422", (0, 0): "--i444"}[(g.subsampling_x, g.subsampling_y)]
        src_pipe, enc_pipe = NamedPipeWriter("src"), NamedPipeWriter("enc")
        self._pipes = [src_pipe, enc_pipe]
        relays = [threading.Thread(target=self._relay, args=(w, p), daemon=True)
                  for w, p in (("src", src_pipe), ("enc", enc_pipe))]
        for t in relays:
            t.start()
        partial = self.table.with_suffix(".partial")
        fps = self.encoded.fps
        model = subprocess.Popen(
            [self.tools.noise_model, f"--width={g.width}", f"--height={g.height}",
             f"--fps={fps.numerator}/{fps.denominator}", f"--bit-depth={g.bit_depth}", chroma,
             f"--input={src_pipe.path}", f"--input-denoised={enc_pipe.path}",
             f"--output-grain-table={partial}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW)
        self.cancel._track(model)
        model_err: deque = deque(maxlen=20)
        threading.Thread(target=_drain, args=(model.stderr, model_err), daemon=True).start()
        try:
            for line in iter(model.stdout.readline, b""):
                if line.startswith(b"Done noise model update"):
                    self.frames_done += 1
            model.wait()
        finally:
            self.cancel._untrack(model)
            for p in self._pipes:
                p.unblock()
            # noise_model stops as soon as the source ends; give the encode's
            # decoder a moment to exit on its own before stopping it.
            for t in relays:
                t.join(timeout=20)
            self._stopping = True
            for proc in self._procs:
                if proc.poll() is None:
                    proc.kill()
            for t in relays:
                t.join(timeout=30)
        self.cancel.check()
        if self.delivered.get("src", 0) != self.delivered.get("enc", 0):
            self._errors.append(f"source and encode delivered different frame counts "
                                f"({self.delivered.get('src', 0)} vs {self.delivered.get('enc', 0)}) "
                                f"for frames {self.start:,}+")
        if model.returncode != 0 or not partial.exists():
            detail = " / ".join(list(model_err)[-3:]) or "no output"
            raise ToolError(f"noise_model failed on frames {self.start:,}-{self.start + self.count:,}: "
                            f"{detail} {' '.join(self._errors)}")
        if self._errors:
            raise ToolError(" ".join(self._errors))
        entries = parse_grain_table(partial.read_text(encoding="ascii"))
        segments = []
        for entry in entries:
            to_frame = lambda ts: int(round(ts * fps.numerator / (fps.denominator * 10_000_000)))
            first = self.start + to_frame(entry.start)
            last = self.start + self.count if entry.end >= 2 ** 62 else self.start + to_frame(entry.end)
            last = min(last, self.start + self.count)
            if last > first:
                segments.append(Segment(first, last, entry.params))
        while self.check_alignment(final=True) is not None:
            pass
        # Stored with frame numbers (not noise_model's chunk-relative times).
        self.table.with_suffix(".json").write_text(json.dumps({"alignment": self.alignment}))
        self.table.write_text(format_grain_table([TableEntry(s.start, s.end, s.params)
                                                  for s in segments]), encoding="ascii")
        partial.unlink()
        self.finished = True
        return segments


# --------------------------------------------------------------------------- driver

def cache_key(source: VideoInfo, encoded: VideoInfo, offset: int, crop: Crop | None,
              noise_model: str) -> str:
    parts = []
    for info in (source, encoded):
        st = info.path.stat()
        parts.append(f"{info.path}|{st.st_size}|{int(st.st_mtime)}")
    parts += [str(offset), str(crop), str(Path(noise_model).stat().st_size), str(CHUNK_FRAMES)]
    return hashlib.sha1("||".join(parts).encode()).hexdigest()[:16]


def plan_chunks(encoded: VideoInfo, source: VideoInfo, offset: int) -> list[tuple[int, int]]:
    first = max(0, -offset)
    last = min(encoded.frame_count, source.frame_count - offset)
    if last - first < 2:
        raise ValueError("the source and encode do not overlap")
    return [(s, min(CHUNK_FRAMES, last - s)) for s in range(first, last, CHUNK_FRAMES)]


def completed_chunks(cache_dir: Path, chunks: list[tuple[int, int]]) -> set[int]:
    return {start for start, _ in chunks if (cache_dir / f"chunk_{start:08d}.tbl").exists()}


def _segment_sigma(p: GrainParams, bit_depth: int) -> float:
    if not p.y_points or not p.apply_grain:
        return 0.0
    lut = synth.scaling_lut(p.y_points)
    return float(lut[128]) / (1 << p.scaling_shift) * synth.luma_template_std(p, bit_depth, seeds=(1,))


def _sigmas_batch(args: tuple[list[GrainParams], int]) -> list[float]:
    params, bit_depth = args
    return [_segment_sigma(p, bit_depth) for p in params]


def segment_sigmas(segments: list[Segment], bit_depth: int, cancel: CancelToken) -> None:
    """Fill in luma_mid_sigma (~18 ms each, so spread over processes)."""
    from concurrent.futures import ProcessPoolExecutor
    batches = [segments[i:i + 200] for i in range(0, len(segments), 200)]
    if len(batches) <= 2:
        for seg in segments:
            seg.luma_mid_sigma = _segment_sigma(seg.params, bit_depth)
        return
    from concurrent.futures.process import BrokenProcessPool
    try:
        with ProcessPoolExecutor(max(2, cpu_count() - 2)) as pool:
            results = pool.map(_sigmas_batch, [([s.params for s in b], bit_depth) for b in batches])
            for batch, sigmas in zip(batches, results):
                cancel.check()
                for seg, sigma in zip(batch, sigmas):
                    seg.luma_mid_sigma = sigma
    except (BrokenProcessPool, OSError):
        # Worker processes could not start (e.g. an unguarded __main__ script);
        # do it here instead, slower but correct.
        for i, seg in enumerate(segments):
            if i % 100 == 0:
                cancel.check()
            seg.luma_mid_sigma = _segment_sigma(seg.params, bit_depth)


def analyze_every_frame(tools: Tools, source: VideoInfo, encoded: VideoInfo, offset: int,
                        crop: Crop | None, cache_dir: Path, progress, cancel: CancelToken,
                        workers: int | None = None, source_hwaccel: bool = True) -> TimelineModel:
    started = time.perf_counter()
    geometry = Geometry.of(encoded)
    chunks = plan_chunks(encoded, source, offset)
    cache_dir.mkdir(parents=True, exist_ok=True)
    done = completed_chunks(cache_dir, chunks)
    todo = [c for c in chunks if c[0] not in done]
    total_frames = sum(n for _, n in chunks)
    resumed_frames = sum(n for s, n in chunks if s in done)
    workers = workers or default_workers()
    runs: list[_ChunkRun] = []
    failure: list[BaseException] = []

    def work(chunk):
        run = _ChunkRun(tools, source, encoded, geometry, crop, offset, chunk[0], chunk[1],
                        cache_dir / f"chunk_{chunk[0]:08d}.tbl", cancel, source_hwaccel)
        runs.append(run)
        return run.run()

    if todo:
        with ThreadPoolExecutor(workers) as pool:
            jobs = [pool.submit(work, c) for c in todo]
            pending = set(jobs)
            clock = time.perf_counter()
            while pending:
                finished = {j for j in pending if j.done()}
                for j in finished:
                    pending.discard(j)
                    if j.exception() and not isinstance(j.exception(), Cancelled):
                        failure.append(j.exception())
                        cancel.cancel()
                for run in list(runs):
                    run.check_alignment()
                    if run.alignment and not run.reported:
                        run.reported = True
                        if run.alignment[0] != "shifted":
                            continue
                        failure.append(RuntimeError(
                            f"Frames {run.start:,}+ do not line up between source and encode "
                            "(they match a neighbouring frame better). Set the frame offset "
                            "(Detect) and start again; finished chunks are kept."))
                        cancel.cancel()
                if failure:
                    break
                current = min(total_frames, resumed_frames + sum(
                    r.count if r.finished else r.frames_done for r in runs))
                elapsed = time.perf_counter() - clock
                new = current - resumed_frames
                rate = new / elapsed if elapsed > 5 and new > 0 else 0.0
                eta = (total_frames - current) / rate if rate else 0.0
                eta_text = ""
                if rate:
                    h, m = divmod(int(eta // 60), 60)
                    eta_text = f", about {h} h {m:02d} min left" if h else f", about {m} min left"
                progress(current / total_frames,
                         f"Analysed {current:,} of {total_frames:,} frames ({rate:.1f} fps{eta_text})")
                time.sleep(0.5)
            for j in pending:
                j.cancel()
        if failure:
            raise failure[0]
        cancel.check()

    progress(0.99, "Summarising the grain over time…")
    segments: list[Segment] = []
    shifted = []
    for start, count in chunks:
        table = cache_dir / f"chunk_{start:08d}.tbl"
        meta = json.loads(table.with_suffix(".json").read_text())
        if meta["alignment"].count("shifted") > len(meta["alignment"]) // 2:
            shifted.append(start)
        for entry in parse_grain_table(table.read_text(encoding="ascii")):
            segments.append(Segment(entry.start, entry.end, entry.params))
    segment_sigmas(segments, geometry.bit_depth, cancel)
    return TimelineModel(segments, geometry.bit_depth, geometry.subsampling_x, geometry.subsampling_y,
                         encoded.fps, encoded.frame_count, time.perf_counter() - started, shifted)
