"""Turn an analysed GrainModel plus user tuning into concrete AFGS1 parameters.

Strength is applied to the target noise standard deviation, and the scaling
curves are then re-fitted, so 200 % really doubles the grain's amplitude
at every brightness (with the scaling shift re-chosen for headroom) instead of
multiplying coded values that saturate at 255.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from . import synth
from .analysis import GrainModel
from .grain_params import MAX_UV_POINTS, MAX_Y_POINTS, GrainParams


@dataclass(frozen=True)
class Tuning:
    luma_strength: float = 1.0
    chroma_strength: float = 1.0
    overlap: bool = True
    clip_to_restricted_range: bool = True
    seed: int = 7391


@dataclass
class BuiltParams:
    params: GrainParams
    clipped: bool                 # a curve needed values above 255 even at shift 8


def fit_points(values: np.ndarray, max_points: int) -> list[tuple[int, int]]:
    """Piecewise-linear fit of a 256-entry curve with at most ``max_points`` knots.

    Greedy refinement: start from the ends, keep adding the x with the largest
    interpolation error until every bin is within one scaling step or 3 % of
    the curve's peak, whichever is larger. The estimate itself is noisier than
    that, so fitting closer would only spend points on noise. Values are
    rounded to 0..255. The curve is first smoothed over +/-7 bins: a median of
    several runs' piecewise curves has small steps where they cross.
    """
    target = np.clip(np.asarray(values, dtype=np.float64), 0, 255)
    if not np.any(target > 0.5):
        return []
    target = np.convolve(np.pad(target, 7, mode="edge"), np.ones(15) / 15, mode="valid")
    tolerance = max(1.0, 0.03 * float(target.max()))
    knots = [0, 255]
    grid = np.arange(256)
    while len(knots) < max_points:
        ys = np.array([round(target[k]) for k in knots], dtype=np.float64)
        approx = np.interp(grid, knots, ys)
        error = np.abs(approx - target)
        error[knots] = 0
        worst = int(np.argmax(error))
        if error[worst] <= tolerance:
            break
        knots = sorted(knots + [worst])
    return [(int(k), int(round(target[k]))) for k in knots]


@lru_cache(maxsize=32)
def _template_stds(key: tuple) -> tuple[float, float, float]:
    p, bit_depth, subx, suby = key[0], key[1], key[2], key[3]
    ty = synth.luma_template_std(p, bit_depth) if p.y_points else 0.0
    tcb, tcr = synth.chroma_template_std(p, bit_depth, subx, suby)
    return ty, tcb, tcr


class _Frozen:
    """Hashable wrapper so the template cache can key on GrainParams contents."""

    def __init__(self, p: GrainParams) -> None:
        self.p = p
        self.key = (p.ar_coeff_lag, tuple(p.ar_coeffs_y), tuple(p.ar_coeffs_cb),
                    tuple(p.ar_coeffs_cr), p.ar_coeff_shift, p.grain_scale_shift,
                    bool(p.y_points), bool(p.cb_points), bool(p.cr_points),
                    p.chroma_scaling_from_luma)

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other) -> bool:
        return isinstance(other, _Frozen) and self.key == other.key

    def __getattr__(self, name):
        return getattr(self.p, name)


def template_stds(model: GrainModel) -> tuple[float, float, float]:
    probe = model.base.copy(y_points=model.base.y_points or [(0, 1)],
                            cb_points=model.base.cb_points or [(0, 1)],
                            cr_points=model.base.cr_points or [(0, 1)])
    return _template_stds((_Frozen(probe), model.bit_depth, model.subsampling_x, model.subsampling_y))


def build_params(model: GrainModel, tuning: Tuning) -> BuiltParams:
    base = model.base
    ty, tcb, tcr = template_stds(model)
    y_sigma = model.luma_sigma * tuning.luma_strength
    cb_sigma = model.cb_sigma * tuning.chroma_strength
    cr_sigma = model.cr_sigma * tuning.chroma_strength

    # Largest shift (finest precision) that keeps every scaling value <= 255.
    need = max(float(np.max(y_sigma / ty)) if ty else 0.0,
               float(np.max(cb_sigma / tcb)) if tcb else 0.0,
               float(np.max(cr_sigma / tcr)) if tcr else 0.0)
    shift = 11
    while shift > 8 and need * (1 << shift) > 255:
        shift -= 1
    clipped = need * (1 << shift) > 255.5

    def curve(sigma: np.ndarray, template: float, limit: int) -> list[tuple[int, int]]:
        if template <= 0:
            return []
        return fit_points(sigma * (1 << shift) / template, limit)

    y_points = curve(y_sigma, ty, MAX_Y_POINTS)
    cb_points = curve(cb_sigma, tcb, MAX_UV_POINTS)
    cr_points = curve(cr_sigma, tcr, MAX_UV_POINTS)
    if bool(cb_points) != bool(cr_points):      # 4:2:0 needs both or neither
        cb_points = cb_points or [(0, 0)]
        cr_points = cr_points or [(0, 0)]
    params = base.copy(
        apply_grain=bool(y_points or cb_points or cr_points),
        y_points=y_points, cb_points=cb_points, cr_points=cr_points,
        chroma_scaling_from_luma=False, scaling_shift=shift,
        overlap_flag=tuning.overlap, clip_to_restricted_range=tuning.clip_to_restricted_range,
        random_seed=tuning.seed & 0xFFFF)
    n_luma = 2 * params.ar_coeff_lag * (params.ar_coeff_lag + 1)
    n_chroma = n_luma + (1 if y_points else 0)
    params.ar_coeffs_y = list(base.ar_coeffs_y[:n_luma]) + [0] * (n_luma - len(base.ar_coeffs_y[:n_luma])) \
        if y_points else []
    for name in ("ar_coeffs_cb", "ar_coeffs_cr"):
        coeffs = list(getattr(base, name))[:n_chroma]
        setattr(params, name, coeffs + [0] * (n_chroma - len(coeffs)))
    params.validate(subsampled_420=(model.subsampling_x, model.subsampling_y) == (1, 1))
    return BuiltParams(params, clipped)


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
    """What grain each picture gets. Same object for frames sharing parameters."""

    def params_at(self, index: int) -> GrainParams:
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

    def clipped_at(self, index: int) -> bool:
        return self.params_at(index) is not None and self._cache[index][2]
