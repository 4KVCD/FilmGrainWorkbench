"""Grain analysis: sample matched frame runs, run AOM noise_model, merge the models.

Pipeline
--------
1. Pick ``runs`` short runs of consecutive frames spread over the video
   (skipping the first/last 5 %, which are usually logos and credits).
   One seek per run instead of one per frame: a seek into a long-GOP VVC
   stream decodes up to a whole GOP, so runs cut decoding work ~10x.
2. Decode each run from the source (cropped/scaled to the encode's picture
   if needed) and from the encode, in parallel, straight to raw files.
3. Check every run is really the same frames in both files: frame k must
   match frame k better than it matches k-1 or k+1.
4. Run ``noise_model --input=source --input-denoised=encode`` on each run in
   parallel. It models the grain the encode lost as AV1 film grain
   parameters.
5. Merge the per-run models into one :class:`GrainModel` by comparing their
   real output noise level at every brightness, not their raw scaling values
   (which mean different things under different AR filters and shifts).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
import json
import math
import tempfile
import time

import numpy as np

from . import synth
from .frames import Crop, Geometry, extract_raw, luma_planes, source_crop_for
from .grain_params import GrainParams, TableEntry, read_grain_table
from .probe import VideoInfo
from .tools import CancelToken, Tools, cpu_count, run


@dataclass
class AnalysisSettings:
    runs: int = 16
    frames_per_run: int = 8
    offset: int = 0              # encoded frame k shows source frame k + offset
    crop: Crop | None = None     # applied to the source before comparison


@dataclass
class RunResult:
    start: int                   # encoded frame index of the run's first frame
    frames: int
    psnr: float                  # mean luma PSNR of matched pairs
    alignment: str               # "ok", "unclear" or "shifted"
    entries: list[TableEntry] = field(default_factory=list)
    luma_sigma: np.ndarray | None = None     # per-run output noise sigma, 256 bins


@dataclass
class GrainModel:
    """What the analysis produced, independent of the user's tuning.

    ``*_sigma`` hold the target noise standard deviation (in sample values at
    ``bit_depth``) for each 8-bit brightness bin 0..255. ``base`` supplies the
    AR filter (grain texture) and chroma index mapping.
    """
    base: GrainParams
    bit_depth: int
    subsampling_x: int
    subsampling_y: int
    luma_sigma: np.ndarray
    cb_sigma: np.ndarray
    cr_sigma: np.ndarray
    runs: list[RunResult] = field(default_factory=list)
    seconds: float = 0.0

    def to_json(self) -> str:
        from .grain_params import format_grain_table
        return json.dumps({
            "version": 1, "bit_depth": self.bit_depth,
            "subsampling": [self.subsampling_x, self.subsampling_y],
            "base_table": format_grain_table([TableEntry(0, 1, self.base)]),
            "luma_sigma": self.luma_sigma.round(4).tolist(),
            "cb_sigma": self.cb_sigma.round(4).tolist(),
            "cr_sigma": self.cr_sigma.round(4).tolist(),
            "runs": [{"start": r.start, "frames": r.frames, "psnr": round(r.psnr, 2),
                      "alignment": r.alignment,
                      "luma_sigma": None if r.luma_sigma is None else r.luma_sigma.round(3).tolist()}
                     for r in self.runs],
        }, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "GrainModel":
        from .grain_params import parse_grain_table
        d = json.loads(text)
        if d.get("version") != 1:
            raise ValueError("unsupported grain model file")
        base = parse_grain_table(d["base_table"])[0].params
        runs = [RunResult(r["start"], r["frames"], r["psnr"], r["alignment"],
                          luma_sigma=None if r.get("luma_sigma") is None else np.array(r["luma_sigma"]))
                for r in d["runs"]]
        return cls(base, d["bit_depth"], *d["subsampling"], np.array(d["luma_sigma"]),
                   np.array(d["cb_sigma"]), np.array(d["cr_sigma"]), runs)


# --------------------------------------------------------------------------- sampling

def plan_runs(encoded: VideoInfo, source: VideoInfo, settings: AnalysisSettings) -> list[int]:
    """Start frames (encoded timeline) of evenly spread runs, avoiding the ends."""
    n = settings.frames_per_run
    total = encoded.frame_count
    last_source = source.frame_count - settings.offset - n
    lo = max(int(total * 0.05), -settings.offset, 0)
    hi = min(int(total * 0.95) - n, last_source, total - n)
    if hi < lo:
        lo, hi = max(0, -settings.offset), max(0, min(total, last_source) - n)
    if hi < lo:
        raise ValueError("the source and encode do not overlap enough to analyse")
    runs = max(1, settings.runs)
    if runs == 1 or hi == lo:
        return [lo]
    step = (hi - lo) / (runs - 1)
    starts = sorted({int(round(lo + i * step)) for i in range(runs)})
    return starts


# --------------------------------------------------------------------------- alignment

def _psnr(a: np.ndarray, b: np.ndarray, peak: float) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse <= 1e-9 else 10 * math.log10(peak * peak / mse)


def check_alignment(src: np.ndarray, enc: np.ndarray, peak: float) -> tuple[float, str]:
    """Return (mean matched PSNR, verdict) for a run of luma planes."""
    matched = [_psnr(s, e, peak) for s, e in zip(src, enc)]
    mean = float(np.mean(matched))
    if len(src) < 3:
        return mean, "unclear"
    earlier = float(np.mean([_psnr(src[i], enc[i + 1], peak) for i in range(len(src) - 1)]))
    later = float(np.mean([_psnr(src[i + 1], enc[i], peak) for i in range(len(src) - 1)]))
    same = float(np.mean(matched[:-1]))
    best_shift = max(earlier, later)
    if best_shift > same + 0.5:
        return mean, "shifted"
    if same > best_shift + 0.5:
        return mean, "ok"
    return mean, "unclear"          # static content: neighbours look the same


def detect_offset(tools: Tools, source: VideoInfo, encoded: VideoInfo, crop: Crop | None,
                  cancel: CancelToken | None = None, search: int = 48,
                  positions: int = 3) -> tuple[int, float]:
    """Find the frame offset between encode and source by matching small luma thumbnails.

    Returns (offset, confidence 0..1). Positive offset means encoded frame k
    shows source frame k + offset (the encode had frames trimmed from the start).
    """
    thumb = Geometry(128, max(2, int(round(128 * encoded.height / encoded.width / 2)) * 2), 8, 1, 1)
    probe_len = 24
    votes: dict[int, int] = {}
    margins = []
    with tempfile.TemporaryDirectory(prefix="afgs1wb-align-") as tmp:
        tmp = Path(tmp)
        for k in range(positions):
            center = int(encoded.frame_count * (0.25 + 0.25 * k))
            enc_start = max(0, min(center, encoded.frame_count - probe_len))
            src_start = max(0, enc_start - search)
            src_count = probe_len + 2 * search
            e_path, s_path = tmp / f"e{k}.yuv", tmp / f"s{k}.yuv"
            ne = extract_raw(tools.ffmpeg, encoded, enc_start, probe_len, e_path, cancel, thumb)
            ns = extract_raw(tools.ffmpeg, source, src_start, src_count, s_path, cancel, thumb, crop)
            if ne < 4 or ns < ne:
                continue
            e = luma_planes(e_path, thumb, ne)
            s = luma_planes(s_path, thumb, ns)
            scores = {}
            for shift in range(0, ns - ne + 1):
                offset = src_start + shift - enc_start
                diff = s[shift:shift + ne] - e
                scores[offset] = float(np.mean(np.abs(diff - diff.mean(axis=(1, 2), keepdims=True))))
            ranked = sorted(scores.items(), key=lambda kv: kv[1])
            best = ranked[0][0]
            votes[best] = votes.get(best, 0) + 1
            if len(ranked) > 1:
                margins.append(ranked[1][1] / max(ranked[0][1], 1e-6))
    if not votes:
        raise RuntimeError("could not decode enough frames to detect the offset")
    offset, count = max(votes.items(), key=lambda kv: kv[1])
    confidence = count / positions * min(1.0, (np.median(margins) - 1.0) if margins else 0.0)
    return offset, float(max(0.0, min(1.0, confidence)))


# --------------------------------------------------------------------------- model merging

def _lut_sigma(points: list[tuple[int, int]], scaling_shift: int, template_std: float) -> np.ndarray:
    return synth.scaling_lut(points).astype(np.float64) / (1 << scaling_shift) * template_std


def entry_sigmas(p: GrainParams, bit_depth: int, subx: int, suby: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Output noise sigma per 8-bit brightness bin for luma, Cb and Cr."""
    ty = synth.luma_template_std(p, bit_depth) if p.y_points else 0.0
    tcb, tcr = synth.chroma_template_std(p, bit_depth, subx, suby)
    y = _lut_sigma(p.y_points, p.scaling_shift, ty)
    if p.chroma_scaling_from_luma:
        cb = _lut_sigma(p.y_points, p.scaling_shift, tcb)
        cr = _lut_sigma(p.y_points, p.scaling_shift, tcr)
    else:
        cb = _lut_sigma(p.cb_points, p.scaling_shift, tcb)
        cr = _lut_sigma(p.cr_points, p.scaling_shift, tcr)
    return y, cb, cr


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    order = np.argsort(values, axis=0)
    v = np.take_along_axis(values, order, axis=0)
    w = weights[order]
    cum = np.cumsum(w, axis=0)
    half = cum[-1] / 2.0
    idx = np.argmax(cum >= half, axis=0)
    return v[idx, np.arange(values.shape[1])]


def merge_entries(entries: list[tuple[GrainParams, float]], bit_depth: int,
                  subx: int, suby: int) -> tuple[GrainParams, np.ndarray, np.ndarray, np.ndarray]:
    """Median noise level per brightness; the AR texture of the most typical entry."""
    curves = [entry_sigmas(p, bit_depth, subx, suby) for p, _ in entries]
    weights = np.array([w for _, w in entries], dtype=np.float64)
    ys = np.array([c[0] for c in curves])
    cbs = np.array([c[1] for c in curves])
    crs = np.array([c[2] for c in curves])
    y = _weighted_median(ys, weights)
    cb = _weighted_median(cbs, weights)
    cr = _weighted_median(crs, weights)
    distance = np.sum((ys - y) ** 2, axis=1) + 0.5 * np.sum((cbs - cb) ** 2 + (crs - cr) ** 2, axis=1)
    medoid = entries[int(np.argmin(distance / np.maximum(weights, 1e-9) ** 0.5))][0]
    return medoid.copy(), y, cb, cr


# --------------------------------------------------------------------------- driver

def _chroma_switch(geometry: Geometry) -> str:
    return {(1, 1): "--i420", (1, 0): "--i422", (0, 0): "--i444"}[
        (geometry.subsampling_x, geometry.subsampling_y)]


def analyze(tools: Tools, source: VideoInfo, encoded: VideoInfo, settings: AnalysisSettings,
            progress, cancel: CancelToken, work_dir: Path | None = None) -> GrainModel:
    """Run the full analysis. ``progress(fraction, message)`` is called from worker threads."""
    started = time.perf_counter()
    geometry = Geometry.of(encoded)
    crop = settings.crop
    starts = plan_runs(encoded, source, settings)
    n = settings.frames_per_run
    decode_workers = max(2, min(8, cpu_count() // 3))
    model_workers = max(2, min(len(starts), cpu_count() - 2))
    total_steps = len(starts) * 3
    done = 0
    peak = float((1 << geometry.bit_depth) - 1)

    def step(message: str) -> None:
        nonlocal done
        done += 1
        progress(done / total_steps, message)

    results: dict[int, RunResult] = {}
    with tempfile.TemporaryDirectory(prefix="afgs1wb-analysis-", dir=work_dir) as tmp:
        tmp = Path(tmp)
        progress(0.0, f"Decoding {len(starts)} runs of {n} frames from both videos…")

        def decode(start: int, which: str) -> tuple[int, str, int]:
            if which == "enc":
                count = extract_raw(tools.ffmpeg, encoded, start, n, tmp / f"e{start}.yuv",
                                    cancel, geometry)
            else:
                count = extract_raw(tools.ffmpeg, source, start + settings.offset, n,
                                    tmp / f"s{start}.yuv", cancel, geometry, crop)
            return start, which, count

        def model(start: int, count: int) -> tuple[int, list[TableEntry]]:
            table = tmp / f"t{start}.tbl"
            run([tools.noise_model, f"--width={geometry.width}", f"--height={geometry.height}",
                 f"--fps={encoded.fps.numerator}/{encoded.fps.denominator}",
                 f"--bit-depth={geometry.bit_depth}", _chroma_switch(geometry),
                 f"--input={tmp / f's{start}.yuv'}", f"--input-denoised={tmp / f'e{start}.yuv'}",
                 f"--output-grain-table={table}"], cancel)
            return start, read_grain_table(table)

        counts: dict[int, dict[str, int]] = {s: {} for s in starts}
        with ThreadPoolExecutor(decode_workers) as decoders, ThreadPoolExecutor(model_workers) as modelers:
            decode_jobs = [decoders.submit(decode, s, w) for s in starts for w in ("enc", "src")]
            model_jobs = []
            for job in as_completed(decode_jobs):
                start, which, count = job.result()
                cancel.check()
                counts[start][which] = count
                step(f"Decoded {'encode' if which == 'enc' else 'source'} run at frame {start:,}")
                if len(counts[start]) == 2:
                    frames = min(counts[start].values())
                    if frames < 2:
                        results[start] = RunResult(start, frames, 0.0, "missing")
                        step(f"Run at frame {start:,} could not be decoded")
                        continue
                    src = luma_planes(tmp / f"s{start}.yuv", geometry, frames, step=2)
                    enc = luma_planes(tmp / f"e{start}.yuv", geometry, frames, step=2)
                    psnr, verdict = check_alignment(src, enc, peak)
                    del src, enc
                    results[start] = RunResult(start, frames, psnr, verdict)
                    if frames < n:
                        for suffix in ("s", "e"):
                            path = tmp / f"{suffix}{start}.yuv"
                            with path.open("r+b") as f:
                                f.truncate(frames * geometry.frame_bytes)
                    model_jobs.append(modelers.submit(model, start, frames))
            for job in as_completed(model_jobs):
                start, entries = job.result()
                cancel.check()
                results[start].entries = entries
                step(f"Estimated grain for run at frame {start:,}")

    runs = [results[s] for s in starts]
    shifted = [r for r in runs if r.alignment == "shifted"]
    if len(shifted) > len(runs) // 3:
        raise RuntimeError(
            f"{len(shifted)} of {len(runs)} sampled runs do not line up frame-for-frame "
            "between source and encode. Use Detect offset (or set the offset by hand) and analyse again.")
    usable = [(r, e) for r in runs if r.alignment != "shifted" and r.entries for e in r.entries]
    if not usable:
        raise RuntimeError("noise_model produced no usable grain estimate")
    fps = float(encoded.fps)
    weighted = []
    for run_result, entry in usable:
        span = entry.end - entry.start if entry.end < 2 ** 62 else 1e7 * run_result.frames / fps
        weighted.append((entry.params, max(span, 1.0)))
    base, y, cb, cr = merge_entries(weighted, geometry.bit_depth,
                                    geometry.subsampling_x, geometry.subsampling_y)
    for run_result in runs:
        if run_result.entries:
            run_result.luma_sigma = entry_sigmas(run_result.entries[-1].params, geometry.bit_depth,
                                                 geometry.subsampling_x, geometry.subsampling_y)[0]
    return GrainModel(base, geometry.bit_depth, geometry.subsampling_x, geometry.subsampling_y,
                      y, cb, cr, runs, time.perf_counter() - started)


def default_crop(source: VideoInfo, encoded: VideoInfo) -> Crop | None:
    return source_crop_for(source, encoded)
