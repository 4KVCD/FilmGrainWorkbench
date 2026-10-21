"""User tuning (strength, flags, seed) applied to measured AFGS1 parameters.

Strength scales the grain's amplitude exactly: the scaling values are
multiplied and the scaling shift re-chosen for headroom, so 200 % doubles the
output noise at every brightness instead of saturating coded values at 255.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import synth
from .grain_params import GrainParams


@dataclass(frozen=True)
class Tuning:
    luma_strength: float = 1.0
    chroma_strength: float = 1.0
    overlap: bool = True                       # libaom's noise model: overlap_flag = 1
    clip_to_restricted_range: bool = False     # ...and clip_to_restricted_range = 0
    seed: int = 7391


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


def sigma_in_8bit_units(sigma: np.ndarray, bit_depth: int) -> np.ndarray:
    return sigma / (1 << (bit_depth - 8))


def scale_params(p: GrainParams, tuning: Tuning, subsampled_420: bool = True) -> tuple[GrainParams, bool]:
    """Scale one parameter set's grain amplitude directly.

    For a fixed AR filter the output sigma is proportional to
    scaling / 2**scaling_shift, so multiplying the scaling values by the
    strength (and re-choosing the shift for headroom) is exact up to rounding.
    Returns (params, clipped).
    """
    luma = [(x, y * tuning.luma_strength) for x, y in p.y_points]
    cb = [(x, y * tuning.chroma_strength) for x, y in p.cb_points]
    cr = [(x, y * tuning.chroma_strength) for x, y in p.cr_points]
    if p.chroma_scaling_from_luma:
        cb = cr = []
    peak = max([y for _, y in luma + cb + cr] + [0.0])
    shift = 11
    while shift > p.scaling_shift - 3 and shift > 8 and peak * (1 << shift) / (1 << p.scaling_shift) > 255:
        shift -= 1
    factor = (1 << shift) / (1 << p.scaling_shift)
    clipped = peak * factor > 255.5

    def quantise(points):
        out = [(x, int(min(255, round(y * factor)))) for x, y in points]
        return out if any(y for _, y in out) else []

    luma_q, cb_q, cr_q = quantise(luma), quantise(cb), quantise(cr)
    if subsampled_420 and bool(cb_q) != bool(cr_q):
        cb_q = cb_q or [(0, 0)]
        cr_q = cr_q or [(0, 0)]
    scaled = p.copy(y_points=luma_q, cb_points=cb_q, cr_points=cr_q,
                    chroma_scaling_from_luma=False, scaling_shift=shift,
                    overlap_flag=tuning.overlap, clip_to_restricted_range=tuning.clip_to_restricted_range,
                    random_seed=tuning.seed & 0xFFFF)
    n_luma = 2 * p.ar_coeff_lag * (p.ar_coeff_lag + 1)
    if not luma_q:
        scaled.ar_coeffs_y = []
        scaled.ar_coeffs_cb = list(p.ar_coeffs_cb[:n_luma])
        scaled.ar_coeffs_cr = list(p.ar_coeffs_cr[:n_luma])
    scaled.apply_grain = bool(luma_q or cb_q or cr_q) and p.apply_grain
    return scaled, clipped


class GrainPlan:
    """What grain each picture gets."""

    def params_at(self, index: int) -> GrainParams | None:
        raise NotImplementedError

    def key_at(self, index: int) -> object:
        """Equal for pictures that get the same parameters (to reuse work done for them)."""
        raise NotImplementedError

    @property
    def clipped(self) -> bool:
        return False


class StaticPlan(GrainPlan):
    def __init__(self, params: GrainParams, clipped: bool = False) -> None:
        self.params = params
        self._clipped = clipped

    def params_at(self, index: int) -> GrainParams:
        return self.params

    def key_at(self, index: int) -> object:
        return 0

    @property
    def clipped(self) -> bool:
        return self._clipped


class TimelinePlan(GrainPlan):
    """Per-segment parameters from an every-frame analysis, scaled on demand."""

    def __init__(self, model, tuning: Tuning) -> None:
        self.model = model
        self.tuning = tuning
        self._cache: dict[int, GrainParams] = {}
        self._any_clipped = False
        self._420 = (model.subsampling_x, model.subsampling_y) == (1, 1)

    def params_at(self, index: int) -> GrainParams:
        segment = self.model.segment_at(index)
        key = segment.start
        if key not in self._cache:
            params, clipped = scale_params(segment.params, self.tuning, self._420)
            self._cache[key] = params
            self._any_clipped |= clipped
        return self._cache[key]

    def key_at(self, index: int) -> object:
        return self.model.segment_at(index).start

    @property
    def clipped(self) -> bool:
        return self._any_clipped


class FramePlan(GrainPlan):
    """Parameters estimated one frame at a time (the viewer's live estimate), scaled on demand.

    ``params_at`` returns None for a frame that has not been estimated yet.
    """

    def __init__(self, estimates: dict[int, GrainParams], tuning: Tuning, subsampled_420: bool) -> None:
        self.estimates = estimates
        self.tuning = tuning
        self._420 = subsampled_420
        self._cache: dict[int, tuple[GrainParams, bool]] = {}

    def params_at(self, index: int) -> GrainParams | None:
        raw = self.estimates.get(index)
        if raw is None:
            return None
        cached = self._cache.get(index)
        if cached is None or cached[0] is not raw:
            cached = self._cache[index] = (raw, *scale_params(raw, self.tuning, self._420))
        return cached[1]

    def key_at(self, index: int) -> object:
        return index

    def clipped_at(self, index: int) -> bool:
        return self.params_at(index) is not None and self._cache[index][2]
