"""VVC's own film grain: VVenC's analysis and vvdec's synthesis on a short clip."""

import glob
import json
import subprocess

import numpy as np
import pytest

from afgs1wb.probe import probe
from afgs1wb.tools import CancelToken
from afgs1wb.vvcgrain import DECODED, GRAIN, VvcSettings, find_vvc_tools, make_clip, read_grain

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
    clip = make_clip(tools.ffmpeg, vvc_tools, info, 8, VvcSettings(fga=fga, qp=28, frames=17), None,
                     tmp_path / "clip", CancelToken(), lambda *_: None)
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


def test_reading_grain_from_nothing_gives_nothing():
    assert read_grain(b"") == []


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
