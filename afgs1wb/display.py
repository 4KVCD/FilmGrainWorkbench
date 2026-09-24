"""YUV planes -> 8-bit RGB for on-screen viewing.

Every view (source, encode, encode + grain) goes through the same conversion,
so flipping between them only ever shows the difference in the pictures.
HDR (PQ / HLG) is tone-mapped to SDR for display; the files are not changed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_MATRICES = {           # (Kr, Kb)
    "bt709": (0.2126, 0.0722),
    "bt2020nc": (0.2627, 0.0593),
    "bt2020c": (0.2627, 0.0593),
    "bt470bg": (0.299, 0.114),
    "smpte170m": (0.299, 0.114),
}
_BT2020_TO_709 = np.array([[1.6605, -0.5876, -0.0728],
                           [-0.1246, 1.1329, -0.0083],
                           [-0.0182, -0.1006, 1.1187]], dtype=np.float32)
_LUT_SIZE = 4096
SDR_WHITE_NITS = 203.0


@dataclass(frozen=True)
class DisplayFormat:
    bit_depth: int
    limited_range: bool
    matrix: str
    transfer: str
    primaries: str
    width: int
    height: int

    @property
    def kr_kb(self) -> tuple[float, float]:
        if self.matrix in _MATRICES:
            return _MATRICES[self.matrix]
        return _MATRICES["bt709" if self.height > 576 else "smpte170m"]


def _pq_to_nits(e: np.ndarray) -> np.ndarray:
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    p = np.power(np.clip(e, 0, 1), 1 / m2)
    return 10000 * np.power(np.maximum(p - c1, 0) / (c2 - c3 * p), 1 / m1)


def _hlg_to_nits(e: np.ndarray) -> np.ndarray:
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    e = np.clip(e, 0, 1)
    lin = np.where(e <= 0.5, e * e / 3, (np.exp((e - c) / a) + b) / 12)
    return 1000 * lin


def _tone_map(nits: np.ndarray, peak: float = 1000.0) -> np.ndarray:
    """Extended Reinhard, 203-nit diffuse white -> 1.0, ``peak`` -> 1.0 compressed."""
    x = nits / SDR_WHITE_NITS
    w = peak / SDR_WHITE_NITS
    return x * (1 + x / (w * w)) / (1 + x)


def _srgb_encode(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * np.power(linear, 1 / 2.4) - 0.055)


class Converter:
    """Precomputes the per-format lookup tables; ``to_rgb`` is then cheap."""

    def __init__(self, fmt: DisplayFormat) -> None:
        self.fmt = fmt
        self.hdr = fmt.transfer in ("smpte2084", "arib-std-b67")
        grid = np.linspace(0, 1, _LUT_SIZE, dtype=np.float64)
        if self.hdr:
            nits = _pq_to_nits(grid) if fmt.transfer == "smpte2084" else _hlg_to_nits(grid)
            self._to_linear = _tone_map(nits).astype(np.float32)
            self._to_srgb = (_srgb_encode(grid) * 255 + 0.5).astype(np.uint8)
            self._gamut = _BT2020_TO_709 if fmt.primaries.startswith("bt2020") else None

    def to_rgb(self, y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        """Return an (H, W, 3) uint8 array."""
        fmt = self.fmt
        scale = float(1 << (fmt.bit_depth - 8))
        sy, sx = y.shape[0] // u.shape[0], y.shape[1] // u.shape[1]
        if sy > 1 or sx > 1:
            u = np.repeat(np.repeat(u, sy, axis=0), sx, axis=1)[:y.shape[0], :y.shape[1]]
            v = np.repeat(np.repeat(v, sy, axis=0), sx, axis=1)[:y.shape[0], :y.shape[1]]
        yf = y.astype(np.float32)
        uf = u.astype(np.float32) - 128 * scale
        vf = v.astype(np.float32) - 128 * scale
        if fmt.limited_range:
            yf = (yf - 16 * scale) / (219 * scale)
            uf /= 224 * scale
            vf /= 224 * scale
        else:
            peak = (1 << fmt.bit_depth) - 1
            yf /= peak
            uf /= peak
            vf /= peak
        kr, kb = fmt.kr_kb
        kg = 1 - kr - kb
        r = yf + 2 * (1 - kr) * vf
        b = yf + 2 * (1 - kb) * uf
        g = (yf - kr * r - kb * b) / kg
        rgb = np.stack((r, g, b), axis=-1)
        if not self.hdr:
            return (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)
        idx = np.clip(rgb * (_LUT_SIZE - 1) + 0.5, 0, _LUT_SIZE - 1).astype(np.int32)
        linear = self._to_linear[idx]
        if self._gamut is not None:
            linear = linear @ self._gamut.T
        idx = np.clip(linear * (_LUT_SIZE - 1) + 0.5, 0, _LUT_SIZE - 1).astype(np.int32)
        return self._to_srgb[idx]


def grain_only(clean: np.ndarray, grained: np.ndarray, bit_depth: int, gain: float = 4.0) -> np.ndarray:
    """Grey image of the added luma grain, amplified ``gain`` times (8-bit)."""
    diff = (grained.astype(np.float32) - clean.astype(np.float32)) / (1 << (bit_depth - 8))
    grey = np.clip(128 + diff * gain, 0, 255).astype(np.uint8)
    return np.repeat(grey[:, :, None], 3, axis=2)
