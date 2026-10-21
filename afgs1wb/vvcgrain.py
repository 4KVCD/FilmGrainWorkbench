"""VVC's own film grain: VVenC's analysis (--fga) and vvdec's synthesis, scene by scene.

A scene (a short run of consecutive source frames) is cut from the source once
and encoded by vvencapp in each film grain analysis mode, which writes the
measured grain as an H.274 film grain characteristics (FGC) SEI message.
vvdecapp decodes every encode twice: without the grain synthesis (the picture
after VVenC removed the grain: its MCTF pre-filter plus coding at the chosen QP)
and with it (what a decoder that synthesises the grain shows). The grain
parameters each frame carries are read from the bitstream (ffmpeg drops the
auto-regressive model).

vvencapp and vvdecapp are looked for in the app's bin folder, then on PATH.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
import os
import re
import shutil
import tempfile
import time
from typing import TextIO

import numpy as np

from .bits import BitReader
from .frames import (PTS_TIME, Crop, Frame, Geometry, ffmpeg_command, frame_index, seek_args,
                     split_planes)
from .paths import bundled_program, missing_program_hint
from .probe import VideoInfo
from .tools import CancelToken, ToolError, run, run_with_log
from .vvc import NAL_PREFIX_SEI, parse_sei_messages, unescape_rbsp

PRESETS = ("faster", "fast", "medium", "slow", "slower")
FGA_MODES = {1: "1 · VTM analysis (JVET-AN0237)",
             2: "2 · calibrated, temporal residual",
             3: "3 · auto-regressive grain model"}
SOURCE, DECODED, GRAIN = "source", "decoded", "grain"
_FILES = {SOURCE: "source.yuv", DECODED: "decoded.yuv", GRAIN: "grain.yuv"}


@dataclass(frozen=True)
class VvcTools:
    vvencapp: str = ""
    vvdecapp: str = ""

    def missing(self) -> list[str]:
        return [name for name, path in (("vvencapp", self.vvencapp), ("vvdecapp", self.vvdecapp))
                if not path or not Path(path).is_file()]


def find_vvc_tools() -> VvcTools:
    """vvencapp / vvdecapp from the app's bin folder (see build.py), else PATH."""
    def find(name: str) -> str:
        local = bundled_program(name)
        return str(local) if local.is_file() else (shutil.which(name) or "")
    return VvcTools(find("vvencapp"), find("vvdecapp"))


@dataclass(frozen=True)
class VvcSettings:
    """What VVenC is asked to do for one encode."""
    fga: int = 2                  # --fga: 1 VTM analysis, 2 calibrated, 3 auto-regressive model
    strength: int = 100           # --fgastrength, percent of the analysed grain
    qp: float = 28.0
    preset: str = "faster"
    frames: int = 33              # scene length: one 32-frame GOP plus the next intra frame

    def coding(self) -> tuple[int, float, str]:
        """Strength, QP and preset: what the three modes' encodes of a scene share.

        The analysis mode and the scene length are not part of it: a scene holds
        an encode per mode, and its length is fixed when it is cut.
        """
        return self.strength, self.qp, self.preset


@dataclass
class GrainComponent:
    """One colour component of an FGC message: intervals of 8-bit intensity and their model values."""
    pieces: list[tuple[int, int, list[int]]]       # (lower, upper, values)


@dataclass
class GrainSet:
    """The film grain parameters a frame carries (an H.274 FGC SEI message)."""
    model_id: int                                  # 0 frequency filtering, 1 auto-regressive
    blending_mode_id: int                          # 0 additive, 1 multiplicative
    log2_scale_factor: int
    components: list[GrainComponent | None]        # Y, Cb, Cr; None = not present

    def describe(self) -> str:
        model = "frequency filtering" if self.model_id == 0 else "auto-regressive"
        blend = "additive" if self.blending_mode_id == 0 else "multiplicative"
        lines = [f"{model} model, {blend} blending, log2_scale_factor {self.log2_scale_factor}"]
        for name, comp in zip(("Y", "Cb", "Cr"), self.components):
            if comp is None or not comp.pieces:
                lines.append(f"{name}: no grain")
                continue
            parts = []
            for lower, upper, values in comp.pieces:
                if self.model_id == 0:
                    # H.274: a missing horizontal cut-off is 8, a missing vertical one equals the horizontal
                    horizontal = values[1] if len(values) > 1 else 8
                    vertical = values[2] if len(values) > 2 else horizontal
                    rest = f" cut-off H{horizontal} V{vertical}"
                else:
                    rest = " AR " + " ".join(str(v) for v in values[1:]) if len(values) > 1 else ""
                parts.append(f"{lower}–{upper}: {values[0] if values else 0}{rest}")
            lines.append(f"{name}: " + "; ".join(parts))
        return "\n".join(lines)


@dataclass
class SceneCut:
    """The source frames of a scene, cut once and encoded by every analysis mode."""
    start: int                                     # first source frame (a cut file may start later)
    count: int
    geometry: Geometry
    crop: Crop | None
    fps: Fraction
    folder: Path
    hdr: str = "off"                               # vvencapp --hdr, from the source's colour tags

    @property
    def source_file(self) -> Path:
        return self.folder / _FILES[SOURCE]


@dataclass
class Clip:
    """A scene encoded and decoded with the given settings; frames live in raw files."""
    start: int                                     # first source frame
    count: int
    geometry: Geometry
    folder: Path                                   # this encode's own files
    settings: VvcSettings
    crop: Crop | None
    fps: float
    source_file: Path                              # the scene's source frames, shared with other modes
    grain: list[GrainSet | None] = field(default_factory=list)
    encode_seconds: float = 0.0

    def covers(self, index: int) -> bool:
        return self.start <= index < self.start + self.count

    @property
    def bitstream(self) -> Path:
        return self.folder / "clip.266"

    @property
    def kbps(self) -> float:
        return self.bitstream.stat().st_size * 8 / 1000 / (self.count / self.fps)

    def frame(self, which: str, index: int) -> Frame:
        g = self.geometry
        k = index - self.start
        if not 0 <= k < self.count:
            raise IndexError(f"frame {index} is not in the clip {self.start}–{self.start + self.count - 1}")
        path = self.source_file if which == SOURCE else self.folder / _FILES[which]
        data = np.fromfile(path, dtype=np.uint16, count=g.frame_bytes // 2, offset=k * g.frame_bytes)
        y, u, v = split_planes(data, g)
        return Frame(index, y, u, v, g)

    def grain_at(self, index: int) -> GrainSet | None:
        k = index - self.start
        return self.grain[k] if 0 <= k < len(self.grain) else None


WORK_PREFIX = "afgs1wb-vvc-"
_IN_USE = ".in-use"


def _in_use(folder: Path) -> bool:
    """Whether a running window still owns this work folder.

    The owner keeps its lock file open and writes its process id into it. On
    Windows an open file cannot be deleted, which answers the question even
    after a crash freed the process id for reuse; elsewhere the id is checked.
    """
    lock = folder / _IN_USE
    if os.name == "nt":
        try:
            lock.unlink(missing_ok=True)
            return False
        except OSError:
            return True
    try:
        pid = int(lock.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return False
    if not pid or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)                            # signal 0: only asks whether the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def open_work_dir(parent: Path | None = None) -> tuple[Path, TextIO]:
    """A new folder for a window's scenes, and its lock file, kept open while the window lives.

    Folders left by earlier runs that ended without cleaning up (a crash, a
    killed process) are deleted first; folders of windows still open are kept.
    Returns (folder, open lock file): close the file before deleting the folder.
    """
    parent = Path(parent or tempfile.gettempdir())
    for old in parent.glob(WORK_PREFIX + "*"):
        if not _in_use(old):
            shutil.rmtree(old, ignore_errors=True)
    folder = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=parent))
    lock = open(folder / _IN_USE, "w", encoding="ascii")
    lock.write(str(os.getpid()))
    lock.flush()
    return folder, lock


def detect_black_bars(ffmpeg: str, info: VideoInfo, cancel: CancelToken | None = None) -> Crop | None:
    """Letterbox / pillarbox borders, from ffmpeg's cropdetect at three places in the video.

    The largest picture area found is kept, so a dark scene cannot crop real
    picture away. Returns None when there are no borders.
    """
    boxes: list[tuple[int, int, int, int]] = []
    for fraction in (0.3, 0.5, 0.7):
        index = int(info.frame_count * fraction)
        command = [ffmpeg, "-hide_banner", "-nostdin", "-copyts", *seek_args(info, index), "-i", str(info.path),
                   "-map", f"0:{info.stream_index}", "-frames:v", "24", "-an", "-sn", "-dn",
                   "-vf", "cropdetect=limit=0.1:round=2:reset=0", "-f", "null", "-"]
        _, log = run_with_log(command, cancel)
        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", log)
        if found:
            boxes.append(tuple(int(v) for v in found[-1]))
    if not boxes:
        return None
    w, h, x, y = max(boxes, key=lambda b: b[0] * b[1])
    if (w, h) == (info.width, info.height) or w < 64 or h < 64:
        return None
    return Crop(w, h, x, y)


SEI_FILM_GRAIN = 19
_VCL_TYPES = range(0, 12)
_IDR_TYPES = (7, 8)                                # IDR_W_RADL, IDR_N_LP
_IRAP_TYPES = (7, 8, 9)                            # ... and CRA
_LEADING_TYPES = (2, 3)                            # RADL, RASL
_NAL_SPS, _NAL_PH = 15, 19


class _Bits(BitReader):
    """BitReader with the Exp-Golomb codes of H.266 headers."""

    def ue(self) -> int:
        zeros = 0
        while self.get(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad Exp-Golomb code")
        return (1 << zeros) - 1 + self.get(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)

    def align(self) -> None:
        self.get(-self.position % 8)


def _annexb_nals(data: bytes) -> list[bytes]:
    """NAL units of an Annex B byte stream (start code prefixed), without their start codes."""
    starts = [m.end() for m in re.finditer(b"\x00\x00\x01", data)]
    return [data[s:(starts[i + 1] - 3 if i + 1 < len(starts) else len(data))].rstrip(b"\x00")
            for i, s in enumerate(starts)]


def _poc_lsb_bits(sps: bytes) -> int:
    """sps_log2_max_pic_order_cnt_lsb_minus4 + 4, read as vvdec reads the SPS (H.266 7.3.2.4)."""
    r = _Bits(unescape_rbsp(sps[2:]))
    r.get(8)                                       # sps id, vps id
    max_sublayers_minus1 = r.get(3)
    r.get(4)                                       # chroma format, CTU size
    if r.get(1):                                   # sps_ptl_dpb_hrd_params_present_flag
        r.get(7 + 1 + 8 + 1 + 1)                   # profile, tier, level, frame only, multilayer
        if r.get(1):                               # gci_present_flag
            r.get(71)                              # the constraint flags and idcs
            r.get(r.get(8))                        # gci_num_reserved_bits, the reserved bits
        r.align()
        present = [r.get(1) for _ in range(max_sublayers_minus1)]
        r.align()
        r.get(8 * sum(present))                    # sublayer_level_idc
        r.get(32 * r.get(8))                       # ptl_num_sub_profiles, general_sub_profile_idc
    r.get(1)                                       # sps_gdr_enabled_flag
    if r.get(1):                                   # sps_ref_pic_resampling_enabled_flag
        r.get(1)
    r.ue(), r.ue()                                 # picture size
    if r.get(1):                                   # conformance window
        for _ in range(4):
            r.ue()
    if r.get(1):
        raise ValueError("subpictures are not supported")
    r.ue()                                         # bit depth
    r.get(2)                                       # entropy coding sync, entry point offsets
    return r.get(4) + 4


def _picture_header(rbsp: bytes, in_slice_header: bool, lsb_bits: int) -> tuple[int, bool]:
    """(ph_pic_order_cnt_lsb, ph_non_ref_pic_flag) from a picture header (H.266 7.3.2.8)."""
    r = _Bits(rbsp)
    if in_slice_header and not r.get(1):           # sh_picture_header_in_slice_header_flag
        raise LookupError("picture header in its own NAL unit")
    irap_or_gdr = r.get(1)
    non_reference = bool(r.get(1))                 # ph_non_ref_pic_flag
    if irap_or_gdr:
        r.get(1)                                   # ph_gdr_pic_flag
    if r.get(1):                                   # ph_inter_slice_allowed_flag
        r.get(1)
    r.ue()                                         # ph_pic_parameter_set_id
    return r.get(lsb_bits), non_reference


def _parse_fgc(payload: bytes) -> tuple[GrainSet | None, bool]:
    """(parameters, or None if cancelled; persistence flag) of a film grain characteristics SEI."""
    r = _Bits(payload)
    if r.get(1):                                   # fg_characteristics_cancel_flag
        return None, False
    model_id = r.get(2)
    if r.get(1):                                   # separate colour description
        r.get(3 + 3 + 1 + 8 + 8 + 8)
    blending = r.get(2)
    log2_scale = r.get(4)
    present = [r.get(1) for _ in range(3)]
    components: list[GrainComponent | None] = []
    for c in range(3):
        if not present[c]:
            components.append(None)
            continue
        intervals, values = r.get(8) + 1, r.get(3) + 1
        pieces = []
        for _ in range(intervals):
            lower, upper = r.get(8), r.get(8)
            pieces.append((lower, upper, [r.se() for _ in range(values)]))
        components.append(GrainComponent(pieces))
    return GrainSet(model_id, blending, log2_scale, components), bool(r.get(1))


def read_grain(bitstream: bytes) -> list[GrainSet | None]:
    """The film grain parameters in force for every picture, in display order.

    FGC SEI messages sit in prefix SEI NAL units in front of their picture. The
    display order comes from each picture's picture order count, derived as
    vvdec does (H.266 8.3.1). A picture without a message keeps the previous
    parameters when their persistence flag says so, until a cancel message or
    the next coded video sequence (H.274).
    """
    lsb_bits = None
    pictures: list[tuple[int, int, list]] = []     # (coded video sequence, POC, its FGC messages)
    header: tuple[int, bool] | None = None         # from a picture header NAL unit, for its slices
    pending: list = []
    cvs, prev_lsb, prev_msb = -1, 0, 0
    for nal in _annexb_nals(bitstream):
        if len(nal) < 2:
            continue
        kind, tid = nal[1] >> 3, (nal[1] & 7) - 1
        if kind == _NAL_SPS:
            lsb_bits = _poc_lsb_bits(nal)
        elif kind == NAL_PREFIX_SEI:
            for ptype, payload in parse_sei_messages(nal) or []:
                if ptype == SEI_FILM_GRAIN:
                    pending.append(_parse_fgc(payload))
        elif kind == _NAL_PH or kind in _VCL_TYPES:
            if lsb_bits is None:
                raise ValueError("a picture comes before the SPS")
            rbsp = unescape_rbsp(nal[2:])
            if kind == _NAL_PH:
                header = _picture_header(rbsp, False, lsb_bits)
                continue
            try:
                lsb, non_reference = _picture_header(rbsp, True, lsb_bits)
            except LookupError:                    # its picture header came in a PH NAL unit
                if header is None:
                    continue                       # a further slice of the same picture
                (lsb, non_reference), header = header, None
            max_lsb = 1 << lsb_bits
            if kind in _IDR_TYPES or (kind in _IRAP_TYPES and cvs < 0):
                cvs, msb = cvs + 1, 0
            elif lsb < prev_lsb and prev_lsb - lsb >= max_lsb // 2:
                msb = prev_msb + max_lsb
            elif lsb > prev_lsb and lsb - prev_lsb > max_lsb // 2:
                msb = prev_msb - max_lsb
            else:
                msb = prev_msb
            if tid == 0 and kind not in _LEADING_TYPES and not non_reference:
                prev_lsb, prev_msb = lsb, msb      # prevTid0Pic, as vvdec's xUpdatePreviousTid0POC
            pictures.append((cvs, msb + lsb, pending))
            pending = []
    result: list[GrainSet | None] = []
    current: GrainSet | None = None
    previous_cvs = None
    for picture_cvs, _, messages in sorted(pictures, key=lambda p: (p[0], p[1])):
        if picture_cvs != previous_cvs:
            current, previous_cvs = None, picture_cvs      # persistence ends with its sequence
        if messages:
            current, persists = messages[-1]
            result.append(current)
            current = current if persists else None
        else:
            result.append(current)
    return result


def cut_scene(ffmpeg: str, info: VideoInfo, start: int, frames: int, crop: Crop | None, folder: Path,
              cancel: CancelToken, progress) -> SceneCut:
    """Write ``frames`` source frames from ``start`` as raw 10-bit 4:2:0, the input of every encode."""
    width, height = (crop.width, crop.height) if crop else (info.width, info.height)
    if width % 2 or height % 2:
        raise ValueError(f"VVenC needs an even picture size, not {width}×{height}")
    geometry = Geometry(width, height, 10, 1, 1)          # VVenC codes 10-bit 4:2:0
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / _FILES[SOURCE]
    progress(-1, f"Reading source frames {start:,}–{start + frames - 1:,}…")
    _, log = run_with_log(ffmpeg_command(ffmpeg, info, start, frames, geometry, crop, str(source),
                                         with_times=True), cancel)
    count = source.stat().st_size // geometry.frame_bytes if source.exists() else 0
    if count == 0:
        raise RuntimeError(f"{info.path.name}: frame {start} could not be decoded")
    times = [float(t) for t in PTS_TIME.findall(log)]
    first = frame_index(info, times[0]) if times else start   # a cut file may start later
    return SceneCut(first, count, geometry, crop, info.fps, folder, vvenc_hdr_mode(info))


def vvenc_hdr_mode(info: VideoInfo) -> str:
    """vvencapp --hdr for the source: an HDR encode adapts its QPs to PQ / HLG, so the preview must too."""
    wide = info.primaries.startswith("bt2020")
    if info.transfer == "smpte2084":
        return "pq_2020" if wide else "pq"
    if info.transfer == "arib-std-b67":
        return "hlg_2020" if wide else "hlg"
    return "off"


def encode_scene(tools: VvcTools, cut: SceneCut, settings: VvcSettings, cancel: CancelToken,
                 progress) -> Clip:
    """Encode a cut scene with VVenC in one analysis mode and decode it twice, in its own folder.

    The folder is deleted again if anything fails or the work is cancelled.
    """
    if tools.missing():
        raise ToolError("Not found: " + ", ".join(tools.missing()) + ". " + missing_program_hint())
    folder = cut.folder / f"fga{settings.fga}-{time.monotonic_ns()}"
    folder.mkdir()
    try:
        g, fps = cut.geometry, cut.fps
        progress(-1, f"Encoding {cut.count} frames with VVenC --fga {settings.fga} (QP {settings.qp:g}, "
                     f"{settings.preset})…")
        started = time.perf_counter()
        run([tools.vvencapp, "-i", str(cut.source_file), "-s", f"{g.width}x{g.height}", "-c", "yuv420_10",
             "--fps", f"{fps.numerator}/{fps.denominator}", "-f", str(cut.count),
             "--qp", f"{settings.qp:g}", "--preset", settings.preset,
             "--fga", str(settings.fga), "--fgastrength", str(settings.strength),
             *(["--hdr", cut.hdr] if cut.hdr != "off" else []),
             "-o", str(folder / "clip.266")], cancel)
        encode_seconds = time.perf_counter() - started

        progress(-1, f"Decoding --fga {settings.fga} with vvdec, without and with its grain synthesis…")

        def decode(which: str, synthesis: int) -> None:
            run([tools.vvdecapp, "-b", str(folder / "clip.266"), "-o", str(folder / _FILES[which]),
                 "-fg", str(synthesis)], cancel)
        with ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(decode, DECODED, 0), pool.submit(decode, GRAIN, 1)]
            grain = read_grain((folder / "clip.266").read_bytes())
            for job in jobs:
                job.result()
        for which in (DECODED, GRAIN):
            size = (folder / _FILES[which]).stat().st_size
            if size != cut.count * g.frame_bytes:
                raise RuntimeError(f"vvdec wrote {size // g.frame_bytes} of {cut.count} frames")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return Clip(cut.start, cut.count, g, folder, settings, cut.crop, float(fps), cut.source_file,
                grain, encode_seconds)


@dataclass
class FrameStats:
    """How much grain VVenC took out of a frame and how much vvdec puts back (8-bit units)."""
    removed: tuple[float, float, float]            # std of source − decoded, Y Cb Cr
    added: tuple[float, float, float]              # std of grained − decoded
    shift: tuple[float, float, float]              # mean of grained − decoded


def frame_stats(source: Frame, decoded: Frame, grained: Frame) -> FrameStats:
    scale = 1 << (decoded.geometry.bit_depth - 8)

    def planes(a: Frame, b: Frame) -> list[np.ndarray]:
        return [(x[::2, ::2].astype(np.float32) - y[::2, ::2]) / scale
                for x, y in ((a.y, b.y), (a.u, b.u), (a.v, b.v))]
    removed = planes(source, decoded)
    added = planes(grained, decoded)
    return FrameStats(tuple(float(np.std(d)) for d in removed), tuple(float(np.std(d)) for d in added),
                      tuple(float(np.mean(d)) for d in added))

