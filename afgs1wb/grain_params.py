"""AV1 film grain parameters and AOM's ``filmgrn1`` grain-table text format.

Field meanings follow the AFGS1 / AV1 film grain semantics:

* scaling points are ``(x, scaling)`` pairs on an 8-bit x axis (0..255);
* AR coefficients are signed (-128..127, i.e. ``ArCoeffs*Plus128 - 128``);
* ``scaling_shift`` and ``ar_coeff_shift`` hold the actual shifts
  (8..11 and 6..9), not the ``*_minus8`` / ``*_minus6`` syntax values;
* ``cb_mult``, ``cb_luma_mult``, ``cr_mult``, ``cr_luma_mult`` (8 bits) and
  ``cb_offset``, ``cr_offset`` (9 bits) hold the coded values, so the
  effective multipliers are ``value - 128`` and offsets ``value - 256``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

MAX_Y_POINTS = 14
MAX_UV_POINTS = 10


def num_luma_ar_coeffs(lag: int) -> int:
    return 2 * lag * (lag + 1)


@dataclass
class GrainParams:
    apply_grain: bool = True
    random_seed: int = 0
    y_points: list[tuple[int, int]] = field(default_factory=list)
    cb_points: list[tuple[int, int]] = field(default_factory=list)
    cr_points: list[tuple[int, int]] = field(default_factory=list)
    chroma_scaling_from_luma: bool = False
    scaling_shift: int = 8
    ar_coeff_lag: int = 0
    ar_coeffs_y: list[int] = field(default_factory=list)
    ar_coeffs_cb: list[int] = field(default_factory=list)
    ar_coeffs_cr: list[int] = field(default_factory=list)
    ar_coeff_shift: int = 6
    grain_scale_shift: int = 0
    cb_mult: int = 128
    cb_luma_mult: int = 192
    cb_offset: int = 256
    cr_mult: int = 128
    cr_luma_mult: int = 192
    cr_offset: int = 256
    overlap_flag: bool = True
    clip_to_restricted_range: bool = False

    def copy(self, **changes) -> "GrainParams":
        clone = replace(self, **changes)
        for name in ("y_points", "cb_points", "cr_points",
                     "ar_coeffs_y", "ar_coeffs_cb", "ar_coeffs_cr"):
            if name not in changes:
                setattr(clone, name, list(getattr(self, name)))
        return clone

    @property
    def num_chroma_ar_coeffs(self) -> int:
        return num_luma_ar_coeffs(self.ar_coeff_lag) + (1 if self.y_points else 0)

    def validate(self, subsampled_420: bool = True) -> None:
        """Raise ValueError if the parameters cannot be coded or synthesized.

        A set with apply_grain off is coded as that flag alone, so its other
        fields do not matter: libaom leaves them all zero for a frame it
        cannot measure (e.g. black frames).
        """
        if not self.apply_grain:
            return
        def check_points(points, limit, name):
            if len(points) > limit:
                raise ValueError(f"{name}: {len(points)} points, at most {limit} allowed")
            previous = -1
            for x, y in points:
                if not (0 <= x <= 255 and 0 <= y <= 255):
                    raise ValueError(f"{name}: point ({x}, {y}) outside 0..255")
                if x <= previous:
                    raise ValueError(f"{name}: x coordinates must strictly increase")
                previous = x

        check_points(self.y_points, MAX_Y_POINTS, "luma scaling")
        check_points(self.cb_points, MAX_UV_POINTS, "Cb scaling")
        check_points(self.cr_points, MAX_UV_POINTS, "Cr scaling")
        if subsampled_420 and not self.chroma_scaling_from_luma and \
                bool(self.cb_points) != bool(self.cr_points):
            raise ValueError("4:2:0 grain needs both or neither of the Cb and Cr curves")
        if not 8 <= self.scaling_shift <= 11:
            raise ValueError("scaling_shift must be 8..11")
        if not 0 <= self.ar_coeff_lag <= 3:
            raise ValueError("ar_coeff_lag must be 0..3")
        if not 6 <= self.ar_coeff_shift <= 9:
            raise ValueError("ar_coeff_shift must be 6..9")
        if not 0 <= self.grain_scale_shift <= 3:
            raise ValueError("grain_scale_shift must be 0..3")
        n_luma = num_luma_ar_coeffs(self.ar_coeff_lag)
        if self.y_points and len(self.ar_coeffs_y) != n_luma:
            raise ValueError(f"expected {n_luma} luma AR coefficients")
        for name in ("ar_coeffs_cb", "ar_coeffs_cr"):
            coeffs = getattr(self, name)
            # Empty means "no chroma AR filtering" and is coded as zeros.
            if len(coeffs) not in (0, self.num_chroma_ar_coeffs):
                raise ValueError(f"expected {self.num_chroma_ar_coeffs} values in {name}")
        for value in (*self.ar_coeffs_y, *self.ar_coeffs_cb, *self.ar_coeffs_cr):
            if not -128 <= value <= 127:
                raise ValueError("AR coefficients must be -128..127")
        for value in (self.cb_mult, self.cb_luma_mult, self.cr_mult, self.cr_luma_mult):
            if not 0 <= value <= 255:
                raise ValueError("chroma multipliers must be 0..255")
        for value in (self.cb_offset, self.cr_offset):
            if not 0 <= value <= 511:
                raise ValueError("chroma offsets must be 0..511")


@dataclass
class TableEntry:
    """One ``E`` record: parameters valid for [start, end) in 100 ns units."""
    start: int
    end: int
    params: GrainParams


def parse_grain_table(text: str) -> list[TableEntry]:
    """Parse the text written by libaom's ``aom_film_grain_table_write``."""
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines or lines[0] != "filmgrn1":
        raise ValueError("not a filmgrn1 grain table")
    entries: list[TableEntry] = []
    previous: GrainParams | None = None
    i = 1

    def take(prefix: str) -> list[int]:
        nonlocal i
        if i >= len(lines):
            raise ValueError(f"grain table ends before its '{prefix}' line")
        parts = lines[i].split()
        i += 1
        if parts[0] != prefix:
            raise ValueError(f"expected a '{prefix}' line, found {parts[0]!r}")
        return [int(v) for v in parts[1:]]

    def points(prefix: str) -> list[tuple[int, int]]:
        values = take(prefix)
        count = values[0]
        if len(values) != 1 + 2 * count:
            raise ValueError(f"'{prefix}' line has the wrong number of values")
        return [(values[1 + 2 * k], values[2 + 2 * k]) for k in range(count)]

    while i < len(lines):
        head = lines[i].split()
        i += 1
        if head[0] != "E" or len(head) != 6:
            raise ValueError(f"malformed grain table entry: {' '.join(head)}")
        start, end, apply_grain, seed, update = (int(v) for v in head[1:])
        if update:
            p = take("p")
            if len(p) != 12:
                raise ValueError("'p' line must have 12 values")
            params = GrainParams(
                ar_coeff_lag=p[0], ar_coeff_shift=p[1], grain_scale_shift=p[2],
                scaling_shift=p[3], chroma_scaling_from_luma=bool(p[4]),
                overlap_flag=bool(p[5]), cb_mult=p[6], cb_luma_mult=p[7],
                cb_offset=p[8], cr_mult=p[9], cr_luma_mult=p[10], cr_offset=p[11])
            params.y_points = points("sY")
            params.cb_points = points("sCb")
            params.cr_points = points("sCr")
            n_luma = num_luma_ar_coeffs(params.ar_coeff_lag)
            params.ar_coeffs_y = take("cY")
            # libaom always writes n_luma + 1 chroma coefficients; the last one
            # (luma correlation) only exists in the bitstream when luma grain does.
            params.ar_coeffs_cb = take("cCb")
            params.ar_coeffs_cr = take("cCr")
            if len(params.ar_coeffs_y) != n_luma or len(params.ar_coeffs_cb) != n_luma + 1 \
                    or len(params.ar_coeffs_cr) != n_luma + 1:
                raise ValueError("AR coefficient count does not match ar_coeff_lag")
            if not params.y_points:
                params.ar_coeffs_cb = params.ar_coeffs_cb[:n_luma]
                params.ar_coeffs_cr = params.ar_coeffs_cr[:n_luma]
        elif previous is None:
            raise ValueError("first grain table entry does not carry parameters")
        else:
            params = previous.copy()
        params.apply_grain = bool(apply_grain)
        params.random_seed = seed & 0xFFFF
        entries.append(TableEntry(start, end, params))
        previous = params
    if not entries:
        raise ValueError("grain table has no entries")
    return entries


def read_grain_table(path: str | Path) -> list[TableEntry]:
    return parse_grain_table(Path(path).read_text(encoding="ascii"))


def format_grain_table(entries: list[TableEntry]) -> str:
    """Write entries in the same layout libaom uses (readable by aomenc/SVT-AV1)."""
    out = ["filmgrn1"]
    for entry in entries:
        p = entry.params
        out.append(f"E {entry.start} {entry.end} {int(p.apply_grain)} {p.random_seed} 1")
        out.append("\tp " + " ".join(str(v) for v in (
            p.ar_coeff_lag, p.ar_coeff_shift, p.grain_scale_shift, p.scaling_shift,
            int(p.chroma_scaling_from_luma), int(p.overlap_flag), p.cb_mult,
            p.cb_luma_mult, p.cb_offset, p.cr_mult, p.cr_luma_mult, p.cr_offset)))
        for prefix, pts in (("sY", p.y_points), ("sCb", p.cb_points), ("sCr", p.cr_points)):
            out.append(f"\t{prefix} {len(pts)}" + "".join(f" {x} {y}" for x, y in pts))
        n_luma = num_luma_ar_coeffs(p.ar_coeff_lag)
        chroma_pad = [0] * (n_luma + 1 - len(p.ar_coeffs_cb))
        out.append("\tcY" + "".join(f" {v}" for v in p.ar_coeffs_y))
        out.append("\tcCb" + "".join(f" {v}" for v in list(p.ar_coeffs_cb) + chroma_pad))
        out.append("\tcCr" + "".join(f" {v}" for v in list(p.ar_coeffs_cr) + chroma_pad))
    return "\n".join(out) + "\n"
