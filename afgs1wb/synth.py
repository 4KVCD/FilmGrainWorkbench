"""AFGS1 / AV1 film grain synthesis (AFGS1 specification, section 8.2).

The functions follow the specification's reference process step by step so
the output can be compared bit-for-bit with FFmpeg's decoder
(tests/test_ffmpeg_roundtrip.py does exactly that). Only the loop structure
is vectorised with NumPy; every rounding and clipping step is the spec's.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .gaussian import GAUSSIAN_SEQUENCE
from .grain_params import GrainParams, num_luma_ar_coeffs

_GAUSS = np.array(GAUSSIAN_SEQUENCE, dtype=np.int32)
LUMA_H, LUMA_W = 73, 82


def _round2(x, n: int):
    if n == 0:
        return x
    return (x + (1 << (n - 1))) >> n


class _Lfsr:
    """The spec's 16-bit RandomRegister and get_random_number()."""

    def __init__(self, seed: int) -> None:
        self.r = seed & 0xFFFF

    def get(self, bits: int) -> int:
        r = self.r
        bit = (r ^ (r >> 1) ^ (r >> 3) ^ (r >> 12)) & 1
        r = (r >> 1) | (bit << 15)
        self.r = r
        return (r >> (16 - bits)) & ((1 << bits) - 1)

    def gaussian_block(self, height: int, width: int) -> np.ndarray:
        idx = np.fromiter((self.get(11) for _ in range(height * width)),
                          dtype=np.int32, count=height * width)
        return _GAUSS[idx].reshape(height, width)


@lru_cache(maxsize=256)
def _white_noise(seed: int, height: int, width: int) -> np.ndarray:
    """The first height*width Gaussian draws for ``seed`` (read-only, cached)."""
    block = _Lfsr(seed).gaussian_block(height, width)
    block.setflags(write=False)
    return block


@dataclass
class GrainTemplates:
    luma: np.ndarray
    cb: np.ndarray
    cr: np.ndarray


def _ar_filter(grain: np.ndarray, coeffs: list[int], lag: int, shift: int,
               gmin: int, gmax: int, luma_term: np.ndarray | None) -> None:
    """Apply the causal AR filter in place (spec 8.2.3).

    ``luma_term`` holds, per output position, the already-averaged luma grain
    that multiplies the final coefficient (chroma only).
    """
    h, w = grain.shape
    n_prev = num_luma_ar_coeffs(lag) - lag     # coefficients on rows above
    for y in range(3, h):
        s = np.zeros(w - 6, dtype=np.int64)
        pos = 0
        for dr in range(-lag, 0):
            row = grain[y + dr]
            for dc in range(-lag, lag + 1):
                c = coeffs[pos]
                if c:
                    s += c * row[3 + dc: w - 3 + dc].astype(np.int64)
                pos += 1
        if luma_term is not None:
            s += coeffs[pos + lag] * luma_term[y - 3].astype(np.int64)
        same_row = coeffs[n_prev: n_prev + lag]      # deltaRow = 0, deltaCol = -lag..-1
        row = grain[y]
        if any(same_row):
            s_list = s.tolist()
            for x in range(3, w - 3):
                acc = s_list[x - 3]
                for k, c in enumerate(same_row):
                    acc += c * int(row[x - lag + k])
                v = int(row[x]) + _round2(acc, shift)
                row[x] = gmin if v < gmin else gmax if v > gmax else v
        else:
            row[3: w - 3] = np.clip(row[3: w - 3] + _round2(s, shift), gmin, gmax)


def generate_grain(p: GrainParams, seed: int, bit_depth: int,
                   subx: int, suby: int) -> GrainTemplates:
    """Spec 8.2.3: white Gaussian noise shaped by the auto-regressive filter."""
    center = 128 << (bit_depth - 8)
    gmin, gmax = -center, (256 << (bit_depth - 8)) - 1 - center
    shift = 12 - bit_depth + p.grain_scale_shift
    ar_shift = p.ar_coeff_shift
    lag = p.ar_coeff_lag

    if p.y_points:
        luma = _round2(_white_noise(seed & 0xFFFF, LUMA_H, LUMA_W), shift).astype(np.int32)
        _ar_filter(luma, list(p.ar_coeffs_y), lag, ar_shift, gmin, gmax, None)
    else:
        luma = np.zeros((LUMA_H, LUMA_W), dtype=np.int32)

    ch, cw = (38 if suby else 73), (44 if subx else 82)
    luma_term = None
    if p.y_points:
        rows = np.arange(3, ch)
        cols = np.arange(3, cw - 3)
        ly = ((rows - 3) << suby) + 3
        lx = ((cols - 3) << subx) + 3
        acc = np.zeros((len(rows), len(cols)), dtype=np.int64)
        for i in range(suby + 1):
            for j in range(subx + 1):
                acc += luma[np.ix_(ly + i, lx + j)]
        luma_term = _round2(acc, subx + suby)

    planes = []
    n_chroma = num_luma_ar_coeffs(lag) + (1 if p.y_points else 0)
    for xor, points, coeffs in ((0xB524, p.cb_points, p.ar_coeffs_cb),
                                (0x49D8, p.cr_points, p.ar_coeffs_cr)):
        if points or p.chroma_scaling_from_luma:
            g = _round2(_white_noise((seed ^ xor) & 0xFFFF, ch, cw), shift).astype(np.int32)
            c = list(coeffs) + [0] * (n_chroma - len(coeffs))
            _ar_filter(g, c, lag, ar_shift, gmin, gmax, luma_term)
        else:
            g = np.zeros((ch, cw), dtype=np.int32)
        planes.append(g)
    return GrainTemplates(luma, planes[0], planes[1])


def scaling_lut(points: list[tuple[int, int]]) -> np.ndarray:
    """Spec 8.2.4: the 256-entry piecewise-linear scaling table."""
    lut = np.zeros(256, dtype=np.int32)
    if not points:
        return lut
    lut[: points[0][0]] = points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dx, dy = x1 - x0, y1 - y0
        delta = dy * ((65536 + (dx >> 1)) // dx)
        for x in range(dx):
            lut[x0 + x] = y0 + ((x * delta + 32768) >> 16)
    lut[points[-1][0]:] = points[-1][1]
    return lut


def _full_lut(lut8: np.ndarray, bit_depth: int) -> np.ndarray:
    """scale_lut() evaluated for every possible sample value."""
    shift = bit_depth - 8
    if shift == 0:
        return lut8.copy()
    index = np.arange(1 << bit_depth)
    x = index >> shift
    rem = index - (x << shift)
    start = lut8[x]
    end = lut8[np.minimum(x + 1, 255)]
    out = start + _round2((end - start) * rem, shift)
    out[x == 255] = lut8[255]
    return out.astype(np.int32)


def _noise_image(t: GrainTemplates, p: GrainParams, seed: int, w: int, h: int,
                 subx: int, suby: int, gmin: int, gmax: int, planes: int) -> list[np.ndarray]:
    """Spec 8.2.5 part 1: assemble 32x32 grain blocks into full-size noise planes."""
    n_stripes = ((h + 1) // 2 + 15) // 16
    blocks_per_stripe = ((w + 1) // 2 + 15) // 16
    templates = (t.luma, t.cb, t.cr)
    stripes = []
    for plane in range(planes):
        psx, psy = (subx, suby) if plane else (0, 0)
        stripes.append(np.zeros((n_stripes, 34 >> psy, (blocks_per_stripe * 32 >> psx) + 34),
                                dtype=np.int32))
    for luma_num in range(n_stripes):
        rng = _Lfsr(seed)
        rng.r ^= ((luma_num * 37 + 178) & 255) << 8
        rng.r ^= (luma_num * 173 + 105) & 255
        for bx in range(blocks_per_stripe):
            x = bx * 16
            rand = rng.get(8)
            off_x, off_y = rand >> 4, rand & 15
            for plane in range(planes):
                psx, psy = (subx, suby) if plane else (0, 0)
                ox = 6 + off_x if psx else 9 + off_x * 2
                oy = 6 + off_y if psy else 9 + off_y * 2
                bh, bw = 34 >> psy, 34 >> psx
                g = templates[plane][oy: oy + bh, ox: ox + bw].copy()
                col = x if psx else x * 2
                dest = stripes[plane][luma_num]
                if p.overlap_flag and x > 0:
                    if psx == 0:
                        old = dest[:bh, col: col + 2]
                        g[:, 0] = old[:, 0] * 27 + g[:, 0] * 17
                        g[:, 1] = old[:, 1] * 17 + g[:, 1] * 27
                        g[:, :2] = np.clip(_round2(g[:, :2], 5), gmin, gmax)
                    else:
                        g[:, 0] = np.clip(_round2(dest[:bh, col] * 23 + g[:, 0] * 22, 5), gmin, gmax)
                dest[:bh, col: col + bw] = g

    images = []
    for plane in range(planes):
        psx, psy = (subx, suby) if plane else (0, 0)
        ph, pw = (h + psy) >> psy, (w + psx) >> psx
        rows_per = 32 >> psy
        img = np.empty((ph, pw), dtype=np.int32)
        s = stripes[plane]
        for luma_num in range((ph + rows_per - 1) // rows_per):
            y0 = luma_num * rows_per
            n = min(rows_per, ph - y0)
            block = s[luma_num, :n, :pw].copy()
            if p.overlap_flag and luma_num > 0:
                prev = s[luma_num - 1]
                if psy == 0:
                    block[0] = np.clip(_round2(prev[32, :pw] * 27 + block[0] * 17, 5), gmin, gmax)
                    if n > 1:
                        block[1] = np.clip(_round2(prev[33, :pw] * 17 + block[1] * 27, 5), gmin, gmax)
                else:
                    block[0] = np.clip(_round2(prev[16, :pw] * 23 + block[0] * 22, 5), gmin, gmax)
            img[y0: y0 + n] = block
        images.append(img)
    return images


def apply_grain(y: np.ndarray, u: np.ndarray | None, v: np.ndarray | None,
                p: GrainParams, seed: int, bit_depth: int, subx: int = 1, suby: int = 1,
                matrix_identity: bool = False, templates: GrainTemplates | None = None
                ) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
    """Return new (Y, U, V) planes with film grain added (spec 8.2).

    Planes are integer arrays at ``bit_depth``. ``templates`` may be passed to
    reuse grain from :func:`generate_grain` for the same params and seed.
    """
    if not p.apply_grain:
        return y, u, v
    h, w = y.shape
    center = 128 << (bit_depth - 8)
    gmin, gmax = -center, (256 << (bit_depth - 8)) - 1 - center
    planes = 1 if u is None else 3
    if templates is None:
        templates = generate_grain(p, seed, bit_depth, subx, suby)
    noise = _noise_image(templates, p, seed, w, h, subx, suby, gmin, gmax, planes)

    if p.clip_to_restricted_range:
        min_value = 16 << (bit_depth - 8)
        max_luma = 235 << (bit_depth - 8)
        max_chroma = max_luma if matrix_identity else 240 << (bit_depth - 8)
    else:
        min_value, max_luma = 0, (256 << (bit_depth - 8)) - 1
        max_chroma = max_luma
    shift = p.scaling_shift
    peak = (1 << bit_depth) - 1
    luma_lut = _full_lut(scaling_lut(p.y_points), bit_depth)
    y32 = y.astype(np.int32)

    out_u, out_v = u, v
    if planes == 3:
        ch, cw = u.shape
        luma_x = np.arange(cw) << subx
        luma_rows = y32[(np.arange(ch) << suby)[:, None], luma_x[None, :]]
        if subx:
            nxt = np.minimum(luma_x + 1, w - 1)
            luma_next = y32[(np.arange(ch) << suby)[:, None], nxt[None, :]]
            avg_luma = _round2(luma_rows + luma_next, 1)
        else:
            avg_luma = luma_rows
        results = []
        for plane, orig, points, mult, luma_mult, offset in (
                (1, u, p.cb_points, p.cb_mult, p.cb_luma_mult, p.cb_offset),
                (2, v, p.cr_points, p.cr_mult, p.cr_luma_mult, p.cr_offset)):
            if not (points or p.chroma_scaling_from_luma):
                results.append(orig)
                continue
            o = orig.astype(np.int32)
            if p.chroma_scaling_from_luma:
                merged = avg_luma
                lut = luma_lut
            else:
                combined = avg_luma * (luma_mult - 128) + o * (mult - 128)
                merged = np.clip((combined >> 6) + (offset - 256) * (1 << (bit_depth - 8)), 0, peak)
                lut = _full_lut(scaling_lut(points), bit_depth)
            n = _round2(lut[merged] * noise[plane][:ch, :cw], shift)
            results.append(np.clip(o + n, min_value, max_chroma).astype(orig.dtype))
        out_u, out_v = results

    out_y = y
    if p.y_points:
        n = _round2(luma_lut[y32] * noise[0][:h, :w], shift)
        out_y = np.clip(y32 + n, min_value, max_luma).astype(y.dtype)
    return out_y, out_u, out_v


def approx_luma_template_std(p: GrainParams, bit_depth: int) -> float:
    """Fast analytical estimate of :func:`luma_template_std`.

    The AR filter is causal, so its white-noise power gain is the mean of
    1/|1 - A(w)|^2 over frequency. Rounding and clipping are ignored, which
    makes this a few percent off - fine for plotting thousands of segments.
    """
    lag = p.ar_coeff_lag
    base = 512.0 / (1 << (12 - bit_depth + p.grain_scale_shift))
    if lag == 0 or not p.ar_coeffs_y:
        return base
    kernel = np.zeros((64, 64))
    pos = 0
    for dr in range(-lag, 1):
        for dc in range(-lag, lag + 1):
            if dr == 0 and dc == 0:
                break
            kernel[dr % 64, dc % 64] = p.ar_coeffs_y[pos] / (1 << p.ar_coeff_shift)
            pos += 1
    response = np.abs(1 - np.fft.fft2(kernel)) ** 2
    gain = float(np.mean(1 / np.maximum(response, 1e-3)))
    return base * min(gain, 64.0) ** 0.5


def luma_template_std(p: GrainParams, bit_depth: int, seeds: tuple[int, ...] = (1, 7391, 40000)) -> float:
    """Standard deviation of the luma grain template in sample units.

    The synthesized luma noise at intensity I has a standard deviation of
    about ``scaling(I) / 2**scaling_shift * luma_template_std``. Analysis uses
    this to compare parameter sets whose AR filters (and so gains) differ.
    """
    values = []
    for seed in seeds:
        t = generate_grain(p, seed, bit_depth, 1, 1).luma
        values.append(t[9:73, 9:73].astype(np.float64).std())
    return float(np.mean(values))


def chroma_template_std(p: GrainParams, bit_depth: int, subx: int, suby: int,
                        seeds: tuple[int, ...] = (1, 7391, 40000)) -> tuple[float, float]:
    cb, cr = [], []
    oy, ox = (6 if suby else 9), (6 if subx else 9)
    hh, ww = (34 >> suby) + 15, (34 >> subx) + 15
    for seed in seeds:
        t = generate_grain(p, seed, bit_depth, subx, suby)
        cb.append(t.cb[oy: oy + hh, ox: ox + ww].astype(np.float64).std())
        cr.append(t.cr[oy: oy + hh, ox: ox + ww].astype(np.float64).std())
    return float(np.mean(cb)), float(np.mean(cr))
