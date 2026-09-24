"""AOM film-grain-table parsing and AFGS1 payload serialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import copy


@dataclass
class GrainParameters:
    apply_grain: int = 1
    random_seed: int = 7391
    ar_coeff_lag: int = 0
    ar_coeff_shift: int = 6
    grain_scale_shift: int = 0
    scaling_shift: int = 8
    chroma_scaling_from_luma: int = 0
    overlap_flag: int = 1
    cb_mult: int = 0
    cb_luma_mult: int = 0
    cb_offset: int = 0
    cr_mult: int = 0
    cr_luma_mult: int = 0
    cr_offset: int = 0
    y_points: list[tuple[int, int]] = field(default_factory=list)
    cb_points: list[tuple[int, int]] = field(default_factory=list)
    cr_points: list[tuple[int, int]] = field(default_factory=list)
    coeff_y: list[int] = field(default_factory=list)
    coeff_cb: list[int] = field(default_factory=list)
    coeff_cr: list[int] = field(default_factory=list)


@dataclass
class GrainEntry:
    start_time: int
    end_time: int
    params: GrainParameters


def _pairs(tokens: list[str], count: int, label: str) -> list[tuple[int, int]]:
    if len(tokens) < count * 2:
        raise ValueError(f"Incomplete {label} scaling points in film-grain table")
    return [(int(tokens[i * 2]), int(tokens[i * 2 + 1])) for i in range(count)]


def parse_aom_table(path: str | Path) -> list[GrainEntry]:
    """Read AOM's human-readable ``filmgrn1`` table format."""
    lines = Path(path).read_text(encoding="ascii", errors="strict").splitlines()
    if not lines or lines[0].strip() != "filmgrn1":
        raise ValueError("The analyzer output is not an AOM filmgrn1 table")

    entries: list[GrainEntry] = []
    previous = GrainParameters()
    i = 1
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        parts = line.split()
        if len(parts) != 6 or parts[0] != "E":
            raise ValueError(f"Malformed film-grain table entry: {line}")
        start, end, apply_grain, seed, update = map(int, parts[1:])
        current = copy.deepcopy(previous)
        current.apply_grain = apply_grain
        current.random_seed = seed & 0xFFFF
        if update:
            if i >= len(lines):
                raise ValueError("Film-grain table ends before parameter line")
            p = lines[i].split()
            i += 1
            if len(p) != 13 or p[0] != "p":
                raise ValueError("Malformed film-grain parameter line")
            (current.ar_coeff_lag, current.ar_coeff_shift,
             current.grain_scale_shift, current.scaling_shift,
             current.chroma_scaling_from_luma, current.overlap_flag,
             current.cb_mult, current.cb_luma_mult, current.cb_offset,
             current.cr_mult, current.cr_luma_mult, current.cr_offset) = map(int, p[1:])

            def read_points(prefix: str) -> list[tuple[int, int]]:
                nonlocal i
                if i >= len(lines):
                    raise ValueError(f"Film-grain table missing {prefix} points")
                row = lines[i].split()
                i += 1
                if not row or row[0] != prefix or len(row) < 2:
                    raise ValueError(f"Malformed {prefix} scaling points")
                n = int(row[1])
                return _pairs(row[2:], n, prefix)

            current.y_points = read_points("sY")
            current.cb_points = read_points("sCb")
            current.cr_points = read_points("sCr")

            n_luma = 2 * current.ar_coeff_lag * (current.ar_coeff_lag + 1)

            def read_coeff(prefix: str, count: int) -> list[int]:
                nonlocal i
                if i >= len(lines):
                    raise ValueError(f"Film-grain table missing {prefix} coefficients")
                row = lines[i].split()
                i += 1
                if not row or row[0] != prefix or len(row) != count + 1:
                    raise ValueError(f"Malformed {prefix} coefficients")
                return list(map(int, row[1:]))

            current.coeff_y = read_coeff("cY", n_luma)
            current.coeff_cb = read_coeff("cCb", n_luma + 1)
            current.coeff_cr = read_coeff("cCr", n_luma + 1)
            previous = copy.deepcopy(current)
        if start < 0 or end <= start:
            raise ValueError("Film-grain table contains an invalid time interval")
        entries.append(GrainEntry(start, end, current))
    if not entries:
        raise ValueError("The analyzer produced an empty film-grain table")
    return entries


def parameter_at(entries: list[GrainEntry], time_100ns: int) -> GrainParameters:
    for entry in entries:
        if entry.start_time <= time_100ns < entry.end_time:
            return entry.params
    if time_100ns < entries[0].start_time:
        return entries[0].params
    return entries[-1].params


class BitWriter:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, width: int) -> None:
        if width < 0 or value < 0 or value >= (1 << width if width else 1):
            raise ValueError(f"Value {value} does not fit in {width} bits")
        self.bits.extend((value >> shift) & 1 for shift in range(width - 1, -1, -1))

    def pad_zero(self) -> None:
        while len(self.bits) % 8:
            self.bits.append(0)

    def bytes(self) -> bytes:
        self.pad_zero()
        out = bytearray()
        for pos in range(0, len(self.bits), 8):
            value = 0
            for bit in self.bits[pos:pos + 8]:
                value = (value << 1) | bit
            out.append(value)
        return bytes(out)


def _scale_points(points: list[tuple[int, int]], gain: float) -> list[tuple[int, int]]:
    return [(x, max(0, min(255, round(y * gain)))) for x, y in points]


def _write_scaling_points(writer: BitWriter, points: list[tuple[int, int]], *, chroma: bool = False) -> None:
    writer.put(len(points), 4)
    if not points and not chroma:
        return
    if len(points) > (10 if chroma else 14):
        raise ValueError(f"AFGS1 supports at most {10 if chroma else 14} {'chroma' if chroma else 'luma'} scaling points")
    # AFGS1 signals the point widths (and the chroma offset) for chroma even
    # when its point count is zero. FFmpeg's AFGS1 parser follows this syntax.
    # Eight bits preserve the AOM table's full 0..255 coordinates and values.
    writer.put(7, 3)  # point_value_increment_bits_minus1
    writer.put(3, 2)  # point_scaling_bits_minus5
    if chroma:
        writer.put(0, 8)  # scaling offset; retain the AOM point values
    if not points:
        return
    previous_x = 0
    for index, (x, y) in enumerate(points):
        increment = x if index == 0 else x - previous_x
        if increment < 0:
            raise ValueError("Scaling point coordinates must be increasing")
        writer.put(increment, 8)
        writer.put(max(0, min(255, y)), 8)
        previous_x = x


def _write_grain_parameters(
    p: GrainParameters,
    width: int,
    height: int,
    bit_depth: int,
    subsampling_x: int,
    subsampling_y: int,
    strength: float,
    seed_offset: int,
    grain_scale_delta: int,
    overlap: bool,
    restricted_range: bool,
) -> list[int]:
    w = BitWriter()
    w.put(0, 3)  # film_grain_param_set_idx
    w.put(1 if p.apply_grain else 0, 1)
    if not p.apply_grain:
        return w.bits

    seed = (p.random_seed + seed_offset) & 0xFFFF
    if seed == 0:
        seed = 1
    w.put(seed, 16)
    w.put(1, 1)  # update_grain_flag: self-contained on every AU
    resolution_log2 = 0
    while (width >> resolution_log2) > 4095 or (height >> resolution_log2) > 4095:
        resolution_log2 += 1
    if resolution_log2 > 15:
        raise ValueError("Picture dimensions cannot be represented in AFGS1")
    w.put(resolution_log2, 4)
    w.put(width >> resolution_log2, 12)
    w.put(height >> resolution_log2, 12)

    monochrome = not (subsampling_x >= 0 and subsampling_y >= 0)
    # This workbench currently analyzes and emits common planar YUV formats.
    w.put(0 if not monochrome else 1, 1)
    if not monochrome:
        w.put(subsampling_x, 1)
        w.put(subsampling_y, 1)
    w.put(1, 1)  # video_signal_characteristics_flag
    w.put(bit_depth - 8, 3)
    w.put(0, 1)  # cicp_info_present_flag; inherit signaling from VVC

    w.put(0, 1)  # predict_scaling_flag
    # predict_y_scaling_flag is inferred as zero when predict_scaling_flag is
    # zero; emitting a bit here would shift every following field.
    y_points = _scale_points(p.y_points, strength)
    _write_scaling_points(w, y_points)

    chroma_from_luma = bool(p.chroma_scaling_from_luma) and not monochrome
    if not monochrome:
        w.put(1 if chroma_from_luma else 0, 1)
    cb_points: list[tuple[int, int]] = [] if chroma_from_luma or monochrome else _scale_points(p.cb_points, strength)
    cr_points: list[tuple[int, int]] = [] if chroma_from_luma or monochrome else _scale_points(p.cr_points, strength)
    if (subsampling_x, subsampling_y) == (1, 1):
        if cb_points and not cr_points:
            cr_points = list(cb_points)
        elif cr_points and not cb_points:
            cb_points = list(cr_points)
    if not chroma_from_luma and not monochrome:
        _write_scaling_points(w, cb_points, chroma=True)
        _write_scaling_points(w, cr_points, chroma=True)

    grain_scaling_minus8 = max(0, min(3, p.scaling_shift - 8))
    w.put(grain_scaling_minus8, 2)
    lag = max(0, min(3, p.ar_coeff_lag))
    w.put(lag, 2)
    n_luma = 2 * lag * (lag + 1)
    n_chroma = n_luma + 1 if y_points else n_luma
    if y_points:
        w.put(3, 2)  # bits_per_ar_coeff_y_minus5: 8-bit syntax
        for coeff in p.coeff_y[:n_luma]:
            # AOM stores signed coefficients [-128, 127]; AFGS1 signals
            # the unsigned value after adding the midpoint (128 for 8 bits).
            w.put(max(0, min(255, coeff + 128)), 8)
        for _ in range(n_luma - len(p.coeff_y[:n_luma])):
            w.put(128, 8)
    if chroma_from_luma or cb_points:
        w.put(3, 2)
        coeffs = p.coeff_cb[:n_chroma]
        for coeff in coeffs:
            w.put(max(0, min(255, coeff + 128)), 8)
        for _ in range(n_chroma - len(coeffs)):
            w.put(128, 8)
    if chroma_from_luma or cr_points:
        w.put(3, 2)
        coeffs = p.coeff_cr[:n_chroma]
        for coeff in coeffs:
            w.put(max(0, min(255, coeff + 128)), 8)
        for _ in range(n_chroma - len(coeffs)):
            w.put(128, 8)

    w.put(max(0, min(3, p.ar_coeff_shift - 6)), 2)
    w.put(max(0, min(3, p.grain_scale_shift + grain_scale_delta)), 2)
    if cb_points:
        w.put(max(0, min(255, p.cb_mult)), 8)
        w.put(max(0, min(255, p.cb_luma_mult)), 8)
        w.put(max(0, min(511, p.cb_offset)), 9)
    if cr_points:
        w.put(max(0, min(255, p.cr_mult)), 8)
        w.put(max(0, min(255, p.cr_luma_mult)), 8)
        w.put(max(0, min(511, p.cr_offset)), 9)
    w.put(1 if overlap else 0, 1)
    w.put(1 if restricted_range else 0, 1)
    return w.bits


def make_afgs1_t35(
    p: GrainParameters,
    width: int,
    height: int,
    bit_depth: int,
    subsampling_x: int,
    subsampling_y: int,
    strength: float = 1.0,
    seed_offset: int = 0,
    grain_scale_delta: int = 0,
    overlap: bool = True,
    restricted_range: bool = False,
) -> bytes:
    """Build the registered T.35 payload specified by AFGS1."""
    params = _write_grain_parameters(p, width, height, bit_depth, subsampling_x,
                                     subsampling_y, strength, seed_offset,
                                     grain_scale_delta, overlap, restricted_range)
    # av1_film_grain_payload: flag, 8-bit byte count, parameters, zero padding.
    # The payload starts after a one- or nine-bit size header, so build it as
    # a continuous bit string and count the byte-aligned total exactly.
    short_size = (3 + len(params) + 7) // 8
    payload = BitWriter()
    if short_size < 4:
        payload.put(1, 1)  # payload_less_than_4byte_flag
        payload.put(short_size, 2)
        payload.bits.extend(params)
        payload.bits.extend([0] * (short_size * 8 - len(payload.bits)))
    else:
        long_size = (9 + len(params) + 7) // 8
        if long_size > 255:
            raise ValueError("AFGS1 film-grain payload exceeds the 8-bit size field")
        payload.put(0, 1)
        payload.put(long_size, 8)
        payload.bits.extend(params)
        payload.bits.extend([0] * (long_size * 8 - len(payload.bits)))
    payload = payload.bytes()
    sets = BitWriter()
    sets.put(1, 1)  # afgs1_enable_flag
    sets.put(0, 4)  # reserved_4bits
    sets.put(0, 3)  # one film-grain set
    for byte in payload:
        sets.put(byte, 8)
    t35 = bytes((0xB5, 0x58, 0x90, 0x01)) + sets.bytes()
    return t35


def make_prefix_sei_nal(t35_payload: bytes) -> bytes:
    """Create a VVC prefix-SEI NAL (type 23) containing one T.35 message."""
    rbsp = bytearray()
    payload_type = 4  # registered ITU-T T.35
    while payload_type >= 255:
        rbsp.append(0xFF)
        payload_type -= 255
    rbsp.append(payload_type)
    payload_size = len(t35_payload)
    while payload_size >= 255:
        rbsp.append(0xFF)
        payload_size -= 255
    rbsp.append(payload_size)
    rbsp.extend(t35_payload)
    rbsp.append(0x80)  # rbsp_trailing_bits

    escaped = bytearray()
    zero_count = 0
    for byte in rbsp:
        if zero_count >= 2 and byte <= 3:
            escaped.append(3)
            zero_count = 0
        escaped.append(byte)
        zero_count = zero_count + 1 if byte == 0 else 0
    # VVC NAL header: forbidden_zero=0, reserved_zero=0, layer=0,
    # nal_unit_type=23 (PREFIX_SEI), temporal_id_plus1=1.
    return b"\x00\xB9" + bytes(escaped)


def preview_summary(entries: list[GrainEntry]) -> str:
    active = sum(1 for e in entries if e.params.apply_grain)
    return f"{len(entries)} time segment(s), {active} with grain enabled"


def serialize_aom_single_entry(p: GrainParameters) -> str:
    """Serialize one parameter interval for VFGS's AOM-table preview input."""
    n_luma = 2 * p.ar_coeff_lag * (p.ar_coeff_lag + 1)
    lines = [
        "filmgrn1",
        f"E 0 9223372036854775807 {int(bool(p.apply_grain))} {p.random_seed & 0xFFFF} 1",
        "p " + " ".join(map(str, (
            p.ar_coeff_lag, p.ar_coeff_shift, p.grain_scale_shift,
            p.scaling_shift, p.chroma_scaling_from_luma, p.overlap_flag,
            p.cb_mult, p.cb_luma_mult, p.cb_offset,
            p.cr_mult, p.cr_luma_mult, p.cr_offset))),
    ]
    for label, points in (("sY", p.y_points), ("sCb", p.cb_points), ("sCr", p.cr_points)):
        values = " ".join(f"{x} {y}" for x, y in points)
        lines.append(f"{label} {len(points)}" + (f" {values}" if values else ""))
    lines.append("cY" + (" " + " ".join(map(str, p.coeff_y[:n_luma])) if n_luma else ""))
    lines.append("cCb " + " ".join(map(str, p.coeff_cb[:n_luma + 1])))
    lines.append("cCr " + " ".join(map(str, p.coeff_cr[:n_luma + 1])))
    return "\n".join(lines) + "\n"
