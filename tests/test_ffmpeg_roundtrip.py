"""End-to-end: write AFGS1 into a real VVC MKV and decode it with ffmpeg."""

import subprocess

import numpy as np

from afgs1wb import synth
from afgs1wb.afgs1 import picture_seed
from afgs1wb.frames import Geometry, read_frame, split_planes
from afgs1wb.mux import write_afgs1_mkv
from afgs1wb.probe import probe
from afgs1wb.tools import CancelToken

from conftest import sample_params


def decode_all(ffmpeg, path, geometry, *extra):
    out = subprocess.run([ffmpeg, "-v", "error", *extra, "-i", str(path), "-map", "0:v:0",
                          "-f", "rawvideo", "-pix_fmt", geometry.pix_fmt, "-"],
                         capture_output=True, check=True).stdout
    fb = geometry.frame_bytes
    return [split_planes(out[i * fb:(i + 1) * fb], geometry) for i in range(len(out) // fb)]


def test_written_file_matches_ffmpeg_and_own_synthesis(tools, need, grainy_clip, tmp_path):
    need("ffmpeg", "ffprobe", "mkvmerge")
    info = probe(grainy_clip, tools.ffprobe)
    params = sample_params()
    output = tmp_path / "out.mkv"
    report = write_afgs1_mkv(tools, info, output, params, 1234, lambda *_: None, CancelToken())
    assert report.pictures == report.packets > 40
    assert report.copied == ["1 audio track", "1 subtitle track"]     # what the write message lists

    streams = subprocess.run([tools.ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
                              "-of", "csv=p=0", str(output)], capture_output=True, text=True).stdout.split()
    assert streams == ["video", "audio", "subtitle"]

    g = Geometry.of(info)
    original = decode_all(tools.ffmpeg, grainy_clip, g)
    clean = decode_all(tools.ffmpeg, output, g, "-export_side_data", "film_grain")
    grained = decode_all(tools.ffmpeg, output, g)
    assert len(original) == len(clean) == len(grained)
    # The bitstream is untouched apart from the SEI...
    for a, b in zip(original, clean):
        assert all(np.array_equal(x, y) for x, y in zip(a, b))
    # ...ffmpeg applies the grain, and our synthesis reproduces it exactly.
    for index, (c, gr) in enumerate(zip(clean, grained)):
        assert not np.array_equal(c[0], gr[0])
        ours = synth.apply_grain(c[0], c[1], c[2], params, picture_seed(1234, index), 10)
        for mine, theirs in zip(ours, gr):
            assert np.array_equal(mine, theirs), f"frame {index} differs from ffmpeg"


def test_read_frame_returns_exact_frame(tools, need, grainy_clip):
    need("ffmpeg", "ffprobe")
    info = probe(grainy_clip, tools.ffprobe)
    g = Geometry.of(info)
    sequential = decode_all(tools.ffmpeg, grainy_clip, g)
    for index in (0, 1, 7, 23, 24, 40, len(sequential) - 1):
        frame = read_frame(tools.ffmpeg, info, index)
        assert np.array_equal(frame.y, sequential[index][0]), f"frame {index}"


def test_first_pts_ignores_decode_order_start_time(tools, need, grainy_clip):
    # libvvenc MKVs report start_time 0.958 s although the first frame is shown at 0.
    need("ffprobe")
    assert probe(grainy_clip, tools.ffprobe).first_pts == 0.0
