"""Every-frame analysis, time-varying parameters and per-frame writing."""

import subprocess

import numpy as np
import pytest

import afgs1wb.fullscan as fullscan
from afgs1wb import synth
from afgs1wb.afgs1 import picture_seed
from afgs1wb.analysis import entry_sigmas
from afgs1wb.frames import Geometry, split_planes
from afgs1wb.mux import write_afgs1_mkv
from afgs1wb.probe import probe
from afgs1wb.tools import Cancelled, CancelToken
from afgs1wb.tuning import TimelinePlan, Tuning, scale_params

from conftest import sample_params


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(fullscan, "CHUNK_FRAMES", 20)


@pytest.fixture
def pair(tools, need, grainy_source, grainy_clip):
    need("ffmpeg", "ffprobe", "noise_model")
    return probe(grainy_source, tools.ffprobe), probe(grainy_clip, tools.ffprobe)


def test_every_frame_covers_all_frames_and_resumes(tools, pair, small_chunks, tmp_path):
    source, encoded = pair
    progress = []
    model = fullscan.analyze_every_frame(tools, source, encoded, 0, None, tmp_path,
                                         lambda f, m: progress.append(f), CancelToken(),
                                         workers=3, source_hwaccel=False)
    frames = min(encoded.frame_count, source.frame_count)
    edges = [(s.start, s.end) for s in model.segments]
    assert edges[0][0] == 0 and edges[-1][1] == frames
    assert all(a[1] == b[0] for a, b in zip(edges, edges[1:]))      # contiguous, no gaps
    assert len({p.name for p in tmp_path.glob("chunk_*.tbl")}) == 3
    assert all(s.luma_mid_sigma > 0 for s in model.segments)
    assert not model.shifted_chunks
    again = fullscan.TimelineModel.from_json(model.to_json())
    assert [(s.start, s.end) for s in again.segments] == edges
    # a second run only reads the finished parts
    resumed = fullscan.analyze_every_frame(tools, source, encoded, 0, None, tmp_path,
                                           lambda *_: None, CancelToken(), workers=3)
    assert [(s.start, s.end) for s in resumed.segments] == edges


def test_wrong_offset_is_caught_early(tools, pair, small_chunks, tmp_path):
    source, encoded = pair
    with pytest.raises(RuntimeError, match="do not line up"):
        fullscan.analyze_every_frame(tools, source, encoded, 2, None, tmp_path, lambda *_: None,
                                     CancelToken(), workers=2, source_hwaccel=False)


def test_cancel_stops_every_frame_analysis(tools, pair, small_chunks, tmp_path):
    source, encoded = pair
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        fullscan.analyze_every_frame(tools, source, encoded, 0, None, tmp_path, lambda *_: None,
                                     token, workers=2, source_hwaccel=False)


@pytest.mark.parametrize("strength", [0.5, 2.0, 3.5])
def test_scale_params_scales_sigma_exactly(strength):
    p = sample_params()
    scaled, clipped = scale_params(p, Tuning(luma_strength=strength, chroma_strength=strength))
    base = entry_sigmas(p, 10, 1, 1)
    new = entry_sigmas(scaled, 10, 1, 1)
    for a, b in zip(base, new):
        assert np.allclose(b, a * strength, rtol=0.03, atol=0.15)
    assert not clipped


def test_timeline_plan_follows_segments():
    a, b = sample_params(), sample_params()
    b.y_points = [(0, 10), (255, 12)]
    model = fullscan.TimelineModel([fullscan.Segment(0, 10, a), fullscan.Segment(10, 30, b)],
                                   10, 1, 1, 24, 30)
    plan = TimelinePlan(model, Tuning())
    assert plan.params_at(3) is plan.params_at(9)
    later = plan.params_at(10)
    assert [y / (1 << later.scaling_shift) for _, y in later.y_points] ==         pytest.approx([y / (1 << b.scaling_shift) for _, y in b.y_points])
    assert plan.params_at(29) is plan.params_at(10)


def test_writer_applies_each_segments_parameters(tools, need, grainy_clip, tmp_path):
    need("ffmpeg", "ffprobe", "mkvmerge")
    encoded = probe(grainy_clip, tools.ffprobe)
    weak, strong = sample_params(), sample_params()
    weak.y_points = [(0, 20), (255, 20)]
    strong.y_points = [(0, 120), (255, 120)]
    model = fullscan.TimelineModel([fullscan.Segment(0, 20, weak), fullscan.Segment(20, 60, strong)],
                                   10, 1, 1, encoded.fps, encoded.frame_count)
    plan = TimelinePlan(model, Tuning(clip_to_restricted_range=True))
    out = tmp_path / "timeline.mkv"
    write_afgs1_mkv(tools, encoded, out, plan, 99, lambda *_: None, CancelToken())
    g = Geometry.of(encoded)

    def decode(path, *extra):
        data = subprocess.run([tools.ffmpeg, "-v", "error", *extra, "-i", str(path), "-map", "0:v:0",
                               "-f", "rawvideo", "-pix_fmt", g.pix_fmt, "-"],
                              capture_output=True, check=True).stdout
        return [split_planes(data[i * g.frame_bytes:(i + 1) * g.frame_bytes], g)
                for i in range(len(data) // g.frame_bytes)]

    clean = decode(out, "-export_side_data", "film_grain")
    grained = decode(out)
    for index in (0, 19, 20, 35):
        mine = synth.apply_grain(*clean[index], plan.params_at(index), picture_seed(99, index), 10)
        assert all(np.array_equal(x, y) for x, y in zip(mine, grained[index])), f"frame {index}"
    weak_amount = np.abs(grained[10][0].astype(int) - clean[10][0]).mean()
    strong_amount = np.abs(grained[30][0].astype(int) - clean[30][0]).mean()
    assert strong_amount > 3 * weak_amount
