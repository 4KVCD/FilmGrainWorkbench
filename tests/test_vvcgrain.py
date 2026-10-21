"""VVC's own film grain: VVenC's analysis and vvdec's synthesis on a short clip."""

import glob
import json
import subprocess

import numpy as np
import pytest

from afgs1wb.bits import BitWriter
from afgs1wb.probe import probe
from afgs1wb.tools import CancelToken
from afgs1wb.vvc import escape_rbsp, sei_nal
from afgs1wb.vvcgrain import (DECODED, GRAIN, GrainComponent, GrainSet, VvcSettings, cut_scene, encode_scene,
                              find_vvc_tools, read_grain)

from conftest import CACHE

FILM = r"E:\Backup of Drive 2\Torrent\The.Beekeeper*REMUX*.mkv"


@pytest.fixture
def vvc_tools(need):
    need("ffmpeg", "ffprobe")
    tools = find_vvc_tools()
    if tools.missing():
        pytest.skip("vvencapp / vvdecapp not built (vvc/build_vvc_tools.ps1)")
    return tools


@pytest.fixture(scope="module")
def film_crop(tools):
    """40 frames, 960x544, of a grainy 4K film. VVenC's analysis measures no grain at all on
    ffmpeg's synthetic noise (white or blurred, over a gradient), so a real film is needed."""
    found = glob.glob(FILM)
    clip = CACHE / "film_crop.mkv"
    if not clip.exists():
        if not found:
            pytest.skip("the film this test cuts from is not on this PC")
        CACHE.mkdir(exist_ok=True)
        subprocess.run([tools.ffmpeg, "-v", "error", "-y", "-ss", "2400", "-i", found[0], "-map", "0:v:0",
                        "-frames:v", "40", "-vf", "crop=960:544:1400:700", "-c:v", "ffv1", str(clip)], check=True)
    return clip


@pytest.mark.parametrize("fga", [2, 3])
def test_clip_carries_and_shows_the_analysed_grain(tools, vvc_tools, film_crop, tmp_path, fga):
    info = probe(film_crop, tools.ffprobe)
    settings = VvcSettings(fga=fga, qp=28, frames=17)
    cut = cut_scene(tools.ffmpeg, info, 8, settings.frames, None, tmp_path / "scene", CancelToken(), lambda *_: None)
    clip = encode_scene(vvc_tools, cut, settings, CancelToken(), lambda *_: None)
    assert (clip.start, clip.count) == (8, 17) and len(clip.grain) == 17
    model = 0 if fga == 2 else 1
    assert all(g is not None and g.model_id == model and g.components[0] for g in clip.grain)
    k = clip.start + 12
    decoded, grained = clip.frame(DECODED, k), clip.frame(GRAIN, k)
    assert np.std(grained.y.astype(float) - decoded.y) > 1.0          # vvdec drew the grain

    if fga == 2:   # ffmpeg reads the frequency model: it must see the same parameters in the same order
        out = subprocess.run([tools.ffprobe, "-v", "error", "-export_side_data", "film_grain", "-show_frames",
                              "-show_entries", "frame=side_data_list", "-of", "json", str(clip.bitstream)],
                             capture_output=True, text=True, check=True).stdout
        frames = json.loads(out)["frames"]
        assert len(frames) == 17
        for side, mine in zip((f["side_data_list"][0] for f in frames), clip.grain):
            theirs = [[(p["intensity_interval_lower_bound"], p["intensity_interval_upper_bound"],
                        [int(v) for v in p["comp_model_value"].split()]) for p in c.get("pieces", [])]
                      for c in side["components"]]
            assert theirs == [c.pieces for c in mine.components if c]    # ffprobe lists present ones
            assert side["log2_scale_factor"] == mine.log2_scale_factor


# ---------------------------------------------------------------- synthetic H.266 streams

def _ue(w: BitWriter, value: int) -> None:
    w.put(0, (value + 1).bit_length() - 1)
    w.put(value + 1, (value + 1).bit_length())


def _nal(kind: int, w: BitWriter) -> bytes:
    w.put(1, 1)                                     # rbsp_stop_one_bit
    return b"\x00\x00\x01" + bytes((0, (kind << 3) | 1)) + escape_rbsp(w.to_bytes())


def _sps(lsb_bits: int) -> bytes:
    """Just the fields read_grain reads, as in an SPS without profile, subpictures or cropping."""
    w = BitWriter()
    w.put(0, 8 + 3 + 4)                             # ids, max sublayers - 1 = 0, chroma format, CTU size
    w.put(0, 3)                                     # no PTL/DPB/HRD, no GDR, no resampling
    _ue(w, 64), _ue(w, 64)
    w.put(0, 2)                                     # no conformance window, no subpictures
    _ue(w, 2)
    w.put(0, 2)
    w.put(lsb_bits - 4, 4)
    return _nal(15, w)


def _picture_header(w: BitWriter, irap: bool, non_reference: bool, lsb: int, lsb_bits: int) -> None:
    w.put(int(irap), 1)
    w.put(int(non_reference), 1)
    if irap:
        w.put(0, 1)                                 # not GDR
    w.put(0, 1)                                     # no inter slices
    _ue(w, 0)                                       # PPS id
    w.put(lsb, lsb_bits)


def _slice(kind: int, lsb: int, lsb_bits: int = 8, non_reference: bool = False) -> bytes:
    w = BitWriter()
    w.put(1, 1)                                     # sh_picture_header_in_slice_header_flag
    _picture_header(w, kind in (7, 8, 9), non_reference, lsb, lsb_bits)
    w.put(0b1011, 4)                                # the rest of the slice
    return _nal(kind, w)


def _picture_header_nal(lsb: int, lsb_bits: int) -> bytes:
    w = BitWriter()
    _picture_header(w, False, False, lsb, lsb_bits)
    return _nal(19, w)


def _slice_after_picture_header(kind: int) -> bytes:
    w = BitWriter()
    w.put(0, 1)                                     # its picture header came in a PH NAL unit
    w.put(0b1011, 4)
    return _nal(kind, w)


def _fgc(mark: int, persist: bool = False, values: tuple = (40,)) -> bytes:
    """A frequency-model FGC SEI with luma only; ``mark`` goes in log2_scale_factor to tell them apart."""
    w = BitWriter()
    w.put(0, 1 + 2 + 1 + 2)                         # not cancelled, model 0, no colour description, additive
    w.put(mark, 4)
    w.put(0b100, 3)                                 # luma only
    w.put(0, 8)                                     # one intensity interval
    w.put(len(values) - 1, 3)
    w.put(16, 8), w.put(235, 8)
    for v in values:
        _ue(w, 2 * v - 1 if v > 0 else -2 * v)      # se(v)
    w.put(int(persist), 1)
    return b"\x00\x00\x01" + sei_nal(19, w.to_bytes(), 0, 1)


_CANCEL = b"\x00\x00\x01" + sei_nal(19, b"\x80", 0, 1)


def _marks(grain) -> list:
    return [g.log2_scale_factor if g else None for g in grain]


def test_grain_follows_display_order_persistence_cancel_and_sequences():
    assert read_grain(b"") == []
    stream = (_sps(8)
              + _fgc(1, persist=True) + _slice(7, 0)        # IDR: 1 persists ...
              + _fgc(2) + _slice(0, 4)                      # coded before 1-3, shown after them
              + _slice(0, 2) + _slice(0, 1) + _slice(0, 3)  # ... over these (display order)
              + _slice(0, 5)                                # 2 did not persist
              + _fgc(3, persist=True) + _slice(0, 6)
              + _CANCEL + _slice(0, 7) + _slice(0, 8)       # a cancel ends 3
              + _fgc(4, persist=True) + _slice(0, 9)
              + _slice(7, 0))                               # a new sequence ends 4
    assert _marks(read_grain(stream)) == [1, 1, 1, 1, 2, None, 3, None, None, 4, None]


def test_picture_order_wraps_and_skips_non_reference_pictures_like_vvdec():
    # 4-bit POC LSBs (wrap at 16). Coded order POC 0, 8, 15 (non-reference), 6, 14, 18, 25 (RASL), 13.
    # 6 is only 6 if POC 15 is not the previous reference picture (else it reads as 22), and
    # 13 only 13 if the leading picture 25 is not (else 29). POC 14 comes as a picture header
    # NAL unit and two slices.
    stream = (_sps(4)
              + _fgc(0) + _slice(7, 0, 4)
              + _fgc(1) + _slice(0, 8, 4)
              + _fgc(2) + _slice(0, 15, 4, non_reference=True)
              + _fgc(3) + _slice(0, 6, 4)
              + _fgc(4) + _picture_header_nal(14, 4) + _slice_after_picture_header(0)
              + _slice_after_picture_header(0)
              + _fgc(5) + _slice(0, 18 % 16, 4)
              + _fgc(6) + _slice(3, 25 % 16, 4)
              + _fgc(7) + _slice(0, 13, 4))
    assert _marks(read_grain(stream)) == [0, 3, 1, 7, 4, 2, 5, 6]


def test_frequency_cut_offs_default_as_h274_says():
    def luma(values) -> str:
        return GrainSet(0, 0, 3, [GrainComponent([(16, 235, list(values))]), None, None]).describe().splitlines()[1]
    assert luma([40]) == "Y: 16–235: 40 cut-off H8 V8"
    assert luma([40, 7]) == "Y: 16–235: 40 cut-off H7 V7"
    assert luma([40, 7, 5]) == "Y: 16–235: 40 cut-off H7 V5"
    auto_regressive = GrainSet(1, 0, 3, [GrainComponent([(0, 255, [40, 3, -2])]), None, None])
    assert auto_regressive.describe().splitlines()[1] == "Y: 0–255: 40 AR 3 -2"
    assert read_grain(_sps(8) + _fgc(0, values=(40, 7, 5)) + _slice(7, 0))[0].components[0].pieces == \
        [(16, 235, [40, 7, 5])]


def test_work_folders_of_crashed_windows_are_deleted_and_open_ones_kept(tmp_path):
    from afgs1wb.vvcgrain import open_work_dir
    crashed = tmp_path / "afgs1wb-vvc-crashed"
    crashed.mkdir()
    (crashed / ".in-use").write_text("")
    (crashed / "source.yuv").write_bytes(b"x")
    first, lock = open_work_dir(tmp_path)
    assert not crashed.exists() and first.exists()
    second, lock2 = open_work_dir(tmp_path)          # another window: the first one's folder stays
    assert first.exists() and second.exists()
    lock.close(), lock2.close()
