"""Checks that source and encode frames are paired correctly."""

from __future__ import annotations

from pathlib import Path
import math
import tempfile

import numpy as np

from .frames import Crop, Geometry, extract_raw, luma_planes
from .probe import VideoInfo
from .tools import CancelToken, Tools


def _psnr(a: np.ndarray, b: np.ndarray, peak: float) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse <= 1e-9 else 10 * math.log10(peak * peak / mse)


def check_alignment(src: np.ndarray, enc: np.ndarray, peak: float,
                    max_shift: int = 3) -> tuple[float, str]:
    """Return (mean matched PSNR, verdict) for a run of luma planes.

    Compares each source frame with the encoded frame shifted by -max_shift..
    +max_shift. "shifted" means some non-zero shift matches clearly better.
    """
    n = len(src)
    matched = [_psnr(s, e, peak) for s, e in zip(src, enc)]
    mean = float(np.mean(matched))
    if n < 3:
        return mean, "unclear"
    scores = {}
    for shift in range(-min(max_shift, n - 2), min(max_shift, n - 2) + 1):
        pairs = [(i, i + shift) for i in range(n) if 0 <= i + shift < n]
        scores[shift] = float(np.mean([_psnr(src[i], enc[j], peak) for i, j in pairs]))
    same = scores.pop(0)
    best_shift = max(scores.values())
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


# --------------------------------------------------------------------------- the frame on screen

MATCH_MARGIN_DB = 1.0       # the paired frame must beat its neighbours by this much
MATCH_FLOOR_DB = 35.0       # below this, a tie with the neighbours means nothing matches


def block_means(luma: np.ndarray, size: int = 8) -> np.ndarray:
    """Luma averaged over size x size blocks: grain averages out, the picture stays."""
    h, w = (luma.shape[0] // size) * size, (luma.shape[1] // size) * size
    return luma[:h, :w].astype(np.float32).reshape(h // size, size, w // size, size).mean(axis=(1, 3))


def frame_match(source: dict[int, np.ndarray], encoded: np.ndarray, paired: int,
                peak: float) -> tuple[str, float | None, float | None]:
    """Is ``encoded`` the source frame ``paired``? Returns (verdict, paired dB, best neighbour dB).

    ``source`` maps source frame numbers to block means (from :func:`block_means`)
    of the paired frame and whichever neighbours are at hand. Comparing block
    means, not pixels, keeps grain the encode removed from reading as a
    mismatch; comparing with the neighbours tells a wrong offset from a still
    shot. Verdicts: "same", "neighbour" (a neighbour matches better),
    "none" (nothing matches), "unclear" (neighbours look alike), "missing".
    """
    if paired not in source:
        return "missing", None, None
    same = _psnr(source[paired], encoded, peak)
    others = [_psnr(source[k], encoded, peak) for k in (paired - 1, paired + 1) if k in source]
    if not others:
        return "unclear", same, None
    best = max(others)
    if best > same + MATCH_MARGIN_DB:
        return "neighbour", same, best
    if same > best + MATCH_MARGIN_DB:
        return "same", same, best
    return ("none" if same < MATCH_FLOOR_DB else "unclear"), same, best
