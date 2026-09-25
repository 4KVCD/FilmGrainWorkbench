"""Encodes cut out of a longer file, like a mkvmerge split of a film.

Such a file starts with leading pictures that reference frames before the cut
(ffmpeg drops them), and its start time is that of the first key frame, not of
the first frame shown. Reads and the every-frame pipeline must still pair each
encoded frame with the right source frame.
"""

import subprocess

import numpy as np
import pytest

import afgs1wb.fullscan as fullscan
from afgs1wb.frames import Geometry, read_frames, split_planes
from afgs1wb.probe import probe
from afgs1wb.tools import CancelToken

from conftest import CACHE, _need

W, H = 320, 184
CUT_OFFSET = 24          # the cut's frame 0 is frame 24 (1.0 s) of the long file


@pytest.fixture(scope="session")
def long_pair(tools):
    """(lossless 4 s source, VVC encode of it cut from 1.5 s by mkvmerge)."""
    _need(tools, "ffmpeg", "ffprobe", "mkvmerge")
    CACHE.mkdir(exist_ok=True)
    source, encode, cut = CACHE / "long_source.mkv", CACHE / "long_vvc.mkv", CACHE / "long_vvc_cut.mkv"
    pattern = ["-f", "lavfi", "-i", "testsrc2=size=320x184:rate=24:duration=4",
               "-vf", "noise=c0s=12:c0f=t+u,format=yuv420p"]
    if not source.exists():
        subprocess.run([tools.ffmpeg, "-v", "error", "-y", *pattern, "-c:v", "ffv1", str(source)], check=True)
    if not encode.exists():
        result = subprocess.run([tools.ffmpeg, "-v", "error", "-y", *pattern, "-c:v", "libvvenc",
                                 "-preset", "faster", "-qp", "37", str(encode)], capture_output=True, text=True)
        if result.returncode:
            pytest.skip("ffmpeg cannot encode VVC here: " + result.stderr[-300:])
    if not cut.exists():
        subprocess.run([tools.mkvmerge, "--quiet", "-o", str(CACHE / "long_vvc_part.mkv"),
                        "--split", "timestamps:00:00:01.5", str(encode)])
        (CACHE / "long_vvc_part-002.mkv").replace(cut)
        (CACHE / "long_vvc_part-001.mkv").unlink()
    return probe(source, tools.ffprobe), probe(cut, tools.ffprobe)


def decode_all(ffmpeg, path):
    g = Geometry(W, H, 10, 1, 1)
    out = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-pix_fmt", g.pix_fmt, "-"], capture_output=True, check=True).stdout
    return [split_planes(out[i * g.frame_bytes:(i + 1) * g.frame_bytes], g)[0]
            for i in range(len(out) // g.frame_bytes)]


def test_cut_file_is_probed_with_its_start_and_frame_count(long_pair):
    _, cut = long_pair
    assert cut.first_pts == 0.0 and cut.start_time > 0.5     # first key frame after leading pictures
    assert cut.frames == cut.frame_count > 40                 # counted by mkvmerge, not from duration


def test_reads_from_a_cut_file_return_the_frame_asked_for(tools, long_pair):
    _, cut = long_pair
    decodable = decode_all(tools.ffmpeg, cut.path)
    first = cut.frame_count - len(decodable)                  # leading pictures ffmpeg drops
    assert first > 0
    frames = read_frames(tools.ffmpeg, cut, 0, 2, target=Geometry(W, H, 10, 1, 1))
    assert [f.index for f in frames] == [first, first + 1]
    for index in (first, first + 1, first + 9, cut.frame_count - 1):
        frame = read_frames(tools.ffmpeg, cut, index, 1, target=Geometry(W, H, 10, 1, 1))[0]
        assert frame.index == index
        assert np.array_equal(frame.y, decodable[index - first]), f"frame {index}"


def test_every_frame_analysis_of_a_cut_pairs_frames_by_time(tools, long_pair, tmp_path):
    analyzer = fullscan.analyzer_path()
    if not analyzer.is_file():
        pytest.skip("grain analyzer not built (python analyzer/build.py)")
    source, cut = long_pair
    model = fullscan.analyze_every_frame(tools, source, cut, CUT_OFFSET, None, tmp_path / "run",
                                         lambda *_: None, CancelToken(), threads=4, source_hwaccel=False)
    decodable = cut.frame_count - len(decode_all(tools.ffmpeg, cut.path))
    assert model.segments[0].start == decodable and model.segments[-1].end == cut.frame_count
    assert "shifted" not in model.alignment and model.notes["warnings"]

    # The same frames paired by hand, through libaom's own sequential path.
    graph = (f"[0:v:0]trim=start_frame={CUT_OFFSET + decodable},format=yuv420p10le,setpts=N[s];"
             f"[1:v:0]format=yuv420p10le,setpts=N[e];[s][e]vstack=inputs=2:shortest=1[o]")
    stacked = subprocess.run([tools.ffmpeg, "-v", "error", "-i", str(source.path), "-i", str(cut.path),
                              "-filter_complex", graph, "-map", "[o]", "-fps_mode", "passthrough",
                              "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-"],
                             capture_output=True, check=True).stdout
    reference = tmp_path / "reference.tbl"
    subprocess.run([str(analyzer), "--width", str(W), "--height", str(H), "--bit-depth", "10",
                    "--chroma", "420", "--start-frame", str(decodable), "--output", str(reference),
                    "--reference"], input=stacked, capture_output=True, check=True)
    assert (tmp_path / "run" / "table.tbl").read_bytes() == reference.read_bytes()
