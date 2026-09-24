"""AFGS1 film grain metadata carried as ITU-T T.35 registered user data.

Implements ``metadata_aom_itu_t_t35()`` from the AOMedia AFGS1 specification
(https://aomediacodec.github.io/afgs1-spec/): country code 0xB5, provider code
0x5890, provider-oriented code 0x01, followed by ``av1_film_grain_param_sets()``.

The writer always emits one complete parameter set (``update_grain_flag = 1``).
FFmpeg (libavcodec/aom_film_grain.c, 2024-2026) drops sets that only update the
seed, so a self-contained set on every picture is the only form it applies.

FFmpeg's parser also reads ``point_c*_value_increment_bits_minus1``,
``point_c*_scaling_bits_minus5`` and ``c*_scaling_offset`` even when the point
count is zero, which the specification does not. The writer therefore never
codes an empty chroma curve: "no chroma grain" becomes one point with a
scaling of 0, which both parsers read identically and which adds no noise.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bits import BitReader, BitWriter
from .grain_params import GrainParams, num_luma_ar_coeffs

T35_PREFIX = bytes((0xB5, 0x58, 0x90, 0x01))
_SILENT_CHROMA = [(0, 0)]


@dataclass(frozen=True)
class PictureFormat:
    width: int
    height: int
    bit_depth: int
    subsampling_x: int = 1
    subsampling_y: int = 1
    monochrome: bool = False


def _bits_for(value: int) -> int:
    return max(1, value.bit_length())


def _resolution_units(width: int, height: int) -> int:
    for log2 in range(16):
        w, h = width >> log2, height >> log2
        if w <= 4095 and h <= 4095:
            if (w << log2, h << log2) != (width, height):
                raise ValueError(f"{width}x{height} cannot be signalled exactly in AFGS1 "
                                 f"(needs units of {1 << log2} samples)")
            return log2
    raise ValueError("picture too large for AFGS1")


def _write_points(w: BitWriter, points: list[tuple[int, int]], chroma: bool) -> None:
    w.put(len(points), 4)
    if not points:
        return
    increments = [points[0][0]] + [points[i][0] - points[i - 1][0] for i in range(1, len(points))]
    bits_incr = max(_bits_for(v) for v in increments)
    offset = min(y for _, y in points) if chroma else 0
    bits_scal = max(5, max(_bits_for(y - offset) for _, y in points))
    w.put(bits_incr - 1, 3)
    w.put(bits_scal - 5, 2)
    if chroma:
        w.put(offset, 8)
    for increment, (_, y) in zip(increments, points):
        w.put(increment, bits_incr)
        w.put(y - offset, bits_scal)


def _write_ar(w: BitWriter, coeffs: list[int]) -> None:
    bits = 5
    while any(not -(1 << (bits - 1)) <= c < (1 << (bits - 1)) for c in coeffs):
        bits += 1
    w.put(bits - 5, 2)
    for c in coeffs:
        w.put(c + (1 << (bits - 1)), bits)


def _film_grain_params_bits(p: GrainParams, fmt: PictureFormat, seed: int) -> BitWriter:
    w = BitWriter()
    w.put(0, 3)                                   # film_grain_param_set_idx
    w.put(int(p.apply_grain), 1)
    if not p.apply_grain:
        return w
    w.put(seed & 0xFFFF, 16)                      # grain_seed
    w.put(1, 1)                                   # update_grain_flag
    log2 = _resolution_units(fmt.width, fmt.height)
    w.put(log2, 4)
    w.put(fmt.width >> log2, 12)
    w.put(fmt.height >> log2, 12)
    w.put(int(fmt.monochrome), 1)                 # luma_only_flag
    if not fmt.monochrome:
        w.put(fmt.subsampling_x, 1)
        w.put(fmt.subsampling_y, 1)
    w.put(1, 1)                                   # video_signal_characteristics_flag
    w.put(fmt.bit_depth - 8, 3)
    w.put(0, 1)                                   # cicp_info_present_flag
    w.put(0, 1)                                   # predict_scaling_flag
    _write_points(w, p.y_points, chroma=False)

    csfl = p.chroma_scaling_from_luma and not fmt.monochrome
    cb_points = cr_points = []
    if not fmt.monochrome:
        w.put(int(csfl), 1)
        if not csfl:
            cb_points = p.cb_points or _SILENT_CHROMA
            cr_points = p.cr_points or _SILENT_CHROMA
            _write_points(w, cb_points, chroma=True)
            _write_points(w, cr_points, chroma=True)

    w.put(p.scaling_shift - 8, 2)                 # grain_scaling_minus8
    w.put(p.ar_coeff_lag, 2)
    n_luma = num_luma_ar_coeffs(p.ar_coeff_lag)
    n_chroma = n_luma + (1 if p.y_points else 0)
    if p.y_points:
        _write_ar(w, list(p.ar_coeffs_y[:n_luma]))
    for points, coeffs in ((cb_points, p.ar_coeffs_cb), (cr_points, p.ar_coeffs_cr)):
        if csfl or points:
            padded = list(coeffs[:n_chroma]) + [0] * (n_chroma - len(coeffs))
            _write_ar(w, padded)
    w.put(p.ar_coeff_shift - 6, 2)
    w.put(p.grain_scale_shift, 2)
    if cb_points:
        w.put(p.cb_mult, 8)
        w.put(p.cb_luma_mult, 8)
        w.put(p.cb_offset, 9)
    if cr_points:
        w.put(p.cr_mult, 8)
        w.put(p.cr_luma_mult, 8)
        w.put(p.cr_offset, 9)
    w.put(int(p.overlap_flag), 1)
    w.put(int(p.clip_to_restricted_range), 1)
    return w


def encode_t35(p: GrainParams, fmt: PictureFormat, seed: int) -> bytes:
    """Build the complete T.35 payload (starting with country code 0xB5)."""
    p.validate(subsampled_420=(fmt.subsampling_x, fmt.subsampling_y) == (1, 1))
    params = _film_grain_params_bits(p, fmt, seed)
    size_bytes = (1 + 8 + len(params) + 7) // 8
    if size_bytes > 255:
        raise ValueError("film grain payload exceeds 255 bytes")
    payload = BitWriter()
    payload.put(0, 1)                             # payload_less_than_4byte_flag
    payload.put(size_bytes, 8)                    # payload_size
    payload.put_bits(params)
    payload.pad_to(size_bytes * 8)

    sets = BitWriter()
    sets.put(1, 1)                                # afgs1_enable_flag
    sets.put(0, 4)                                # reserved_4bits
    sets.put(0, 3)                                # num_film_grain_sets_minus1
    sets.put_bits(payload)
    return T35_PREFIX + sets.to_bytes()


# grain_seed sits after: 32 prefix bits, 8 bits of set header, then
# payload_less_than_4byte_flag (1) + payload_size (8) + set_idx (3) + apply (1).
_SEED_BIT = 8 * len(T35_PREFIX) + 8 + 13


class T35Template:
    """Encode the parameters once, then stamp a per-picture seed into a copy.

    Every picture needs its own grain seed or the grain freezes; re-encoding
    ~150k pictures of a feature film bit by bit would be needlessly slow.
    """

    def __init__(self, p: GrainParams, fmt: PictureFormat) -> None:
        self.payload = encode_t35(p, fmt, 0)
        self.apply = p.apply_grain
        self._as_int = int.from_bytes(self.payload, "big")
        self._shift = len(self.payload) * 8 - _SEED_BIT - 16

    def for_seed(self, seed: int) -> bytes:
        if not self.apply:
            return self.payload
        value = self._as_int | ((seed & 0xFFFF) << self._shift)
        return value.to_bytes(len(self.payload), "big")


def picture_seed(base_seed: int, picture_index: int) -> int:
    """Grain seed for the picture shown at ``picture_index`` (never zero).

    A zero seed would leave the 16-bit LFSR stuck at zero. The same function
    is used when writing metadata and when previewing, so the preview shows
    exactly the grain pattern a decoder will produce for that frame.
    """
    return (base_seed + 40503 * picture_index) % 65535 + 1


@dataclass
class DecodedSet:
    params: GrainParams
    format: PictureFormat
    seed: int


def _read_points(r: BitReader, chroma: bool) -> list[tuple[int, int]]:
    count = r.get(4)
    if not count:
        return []
    bits_incr = r.get(3) + 1
    bits_scal = r.get(2) + 5
    offset = r.get(8) if chroma else 0
    points, x = [], 0
    for i in range(count):
        increment = r.get(bits_incr)
        x = increment if i == 0 else x + increment
        points.append((x, r.get(bits_scal) + offset))
    return points


def _read_ar(r: BitReader, count: int) -> list[int]:
    bits = r.get(2) + 5
    return [r.get(bits) - (1 << (bits - 1)) for _ in range(count)]


def decode_t35(data: bytes) -> list[DecodedSet]:
    """Parse an AFGS1 T.35 payload strictly per the specification.

    Only self-contained sets (``update_grain_flag = 1``, no scaling
    prediction) are supported, which is everything this module writes.
    """
    if not data.startswith(T35_PREFIX):
        raise ValueError("not an AOMedia AFGS1 T.35 payload")
    r = BitReader(data[len(T35_PREFIX):])
    if not r.get(1):
        return []
    r.get(4)
    count = r.get(3) + 1
    out: list[DecodedSet] = []
    for _ in range(count):
        start = r.position
        short = r.get(1)
        size = r.get(2 if short else 8)
        r.get(3)                                  # film_grain_param_set_idx
        p = GrainParams(apply_grain=bool(r.get(1)))
        fmt = PictureFormat(0, 0, 0)
        seed = 0
        if p.apply_grain:
            seed = r.get(16)
            if not r.get(1):
                raise NotImplementedError("update_grain_flag = 0 is not supported")
            log2 = r.get(4)
            width, height = r.get(12) << log2, r.get(12) << log2
            luma_only = bool(r.get(1))
            subx = suby = 0
            if not luma_only:
                subx, suby = r.get(1), r.get(1)
            bit_depth = 0
            if r.get(1):
                bit_depth = r.get(3) + 8
                if r.get(1):
                    r.get(8), r.get(8), r.get(8), r.get(1)
            if r.get(1):
                raise NotImplementedError("predicted scaling functions are not supported")
            fmt = PictureFormat(width, height, bit_depth, subx, suby, luma_only)
            p.y_points = _read_points(r, chroma=False)
            if not luma_only:
                p.chroma_scaling_from_luma = bool(r.get(1))
                if not p.chroma_scaling_from_luma:
                    p.cb_points = _read_points(r, chroma=True)
                    p.cr_points = _read_points(r, chroma=True)
            p.scaling_shift = r.get(2) + 8
            p.ar_coeff_lag = r.get(2)
            n_luma = num_luma_ar_coeffs(p.ar_coeff_lag)
            n_chroma = n_luma + (1 if p.y_points else 0)
            if p.y_points:
                p.ar_coeffs_y = _read_ar(r, n_luma)
            if p.chroma_scaling_from_luma or p.cb_points:
                p.ar_coeffs_cb = _read_ar(r, n_chroma)
            if p.chroma_scaling_from_luma or p.cr_points:
                p.ar_coeffs_cr = _read_ar(r, n_chroma)
            p.ar_coeff_shift = r.get(2) + 6
            p.grain_scale_shift = r.get(2)
            if p.cb_points:
                p.cb_mult, p.cb_luma_mult, p.cb_offset = r.get(8), r.get(8), r.get(9)
            if p.cr_points:
                p.cr_mult, p.cr_luma_mult, p.cr_offset = r.get(8), r.get(8), r.get(9)
            p.overlap_flag = bool(r.get(1))
            p.clip_to_restricted_range = bool(r.get(1))
        used = r.position - start
        if used > size * 8:
            raise ValueError("film grain parameters overrun payload_size")
        r.get(size * 8 - used)
        p.random_seed = seed
        out.append(DecodedSet(p, fmt, seed))
    return out
