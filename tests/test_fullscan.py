"""Every-frame analysis (threaded libaom noise model), time-varying parameters, per-frame writing."""

import subprocess

import numpy as np
import pytest

import afgs1wb.fullscan as fullscan
from afgs1wb import synth
from afgs1wb.afgs1 import picture_seed
from afgs1wb.frames import Geometry, split_planes
from afgs1wb.mux import write_afgs1_mkv
from afgs1wb.probe import probe
from afgs1wb.tools import Cancelled, CancelToken
from afgs1wb.tuning import TimelinePlan, Tuning, entry_sigmas, scale_params

from conftest import sample_params

W, H = 320, 184


@pytest.fixture
def analyzer():
    path = fullscan.analyzer_path()
    if not path.is_file():
        pytest.skip("grain analyzer not built (python analyzer/build.py)")
    return path


@pytest.fixture
def pair(tools, need, grainy_source, grainy_clip):
    need("ffmpeg", "ffprobe")
    return probe(grainy_source, tools.ffprobe), probe(grainy_clip, tools.ffprobe)


@pytest.fixture
def stacked(tools, need, grainy_source, grainy_clip, tmp_path):
    """Source above encode, as the app feeds the analyzer."""
    need("ffmpeg")
    graph = ("[0:v:0]format=yuv420p10le,settb=1/24,setpts=N[s];[1:v:0]format=yuv420p10le,"
             "settb=1/24,setpts=N[e];[s][e]vstack=inputs=2:shortest=1[o]")
    out = tmp_path / "stack.yuv"
    subprocess.run([tools.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(grainy_source),
                    "-i", str(grainy_clip), "-filter_complex", graph, "-map", "[o]",
                    "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p10le", str(out)],
                   check=True)
    return out


def run_analyzer(analyzer, data: bytes, output, *extra):
    result = subprocess.run([str(analyzer), "--width", str(W), "--height", str(H), "--bit-depth", "10",
                             "--chroma", "420", "--output", str(output), *extra],
                            input=data, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout.decode()


def test_threads_give_exactly_libaoms_sequential_result(analyzer, stacked, tmp_path):
    data = stacked.read_bytes()
    run_analyzer(analyzer, data, tmp_path / "ref.tbl", "--reference")
    reference = (tmp_path / "ref.tbl").read_bytes()
    assert reference.count(b"\nE ") >= 1
    for threads in (1, 3, 16):
        run_analyzer(analyzer, data, tmp_path / f"t{threads}.tbl", "--threads", str(threads))
        assert (tmp_path / f"t{threads}.tbl").read_bytes() == reference, f"{threads} threads"


def test_resumed_run_equals_uninterrupted_run(analyzer, stacked, tmp_path):
    data = stacked.read_bytes()
    frame = 2 * (W * H + 2 * (W // 2) * (H // 2)) * 2
    run_analyzer(analyzer, data, tmp_path / "full.tbl", "--threads", "4")
    # first attempt stops after 20 frames with a checkpoint every 16
    ckpt = tmp_path / "ckpt.bin"
    run_analyzer(analyzer, data[:20 * frame], tmp_path / "part.tbl", "--threads", "4",
                 "--checkpoint", str(ckpt), "--checkpoint-every", "16")
    assert fullscan.checkpoint_frame(ckpt) == 16
    run_analyzer(analyzer, data[16 * frame:], tmp_path / "part.tbl", "--threads", "4",
                 "--start-frame", "16", "--resume", str(ckpt), "--checkpoint", str(ckpt))
    assert (tmp_path / "part.tbl").read_bytes() == (tmp_path / "full.tbl").read_bytes()


def test_every_frame_covers_all_frames_and_is_reused(tools, analyzer, pair, tmp_path):
    source, encoded = pair
    progress = []
    model = fullscan.analyze_every_frame(tools, source, encoded, 0, None, tmp_path,
                                         lambda f, m: progress.append(m), CancelToken(),
                                         threads=4, source_hwaccel=False)
    first, last = fullscan.frame_range(encoded, source, 0)
    edges = [(s.start, s.end) for s in model.segments]
    assert edges[0][0] == first and edges[-1][1] == last
    assert all(a[1] == b[0] for a, b in zip(edges, edges[1:]))       # contiguous, no gaps
    assert "shifted" not in model.alignment
    assert model.sigma_values and max(model.sigma_values) > 0
    assert (tmp_path / "done.json").exists() and not (tmp_path / "checkpoint.bin").exists()
    again = fullscan.TimelineModel.from_json(model.to_json())
    assert [(s.start, s.end) for s in again.segments] == edges
    reused = fullscan.analyze_every_frame(tools, source, encoded, 0, None, tmp_path,
                                          lambda *_: None, CancelToken(), threads=4)
    assert [(s.start, s.end) for s in reused.segments] == edges


def test_wrong_offset_is_caught_before_analysis(tools, analyzer, pair, tmp_path):
    source, encoded = pair
    with pytest.raises(RuntimeError, match="do not line up"):
        fullscan.analyze_every_frame(tools, source, encoded, 2, None, tmp_path, lambda *_: None,
                                     CancelToken(), threads=2, source_hwaccel=False)
    assert not (tmp_path / "table.tbl").exists()


def test_cancel_stops_every_frame_analysis(tools, analyzer, pair, tmp_path):
    source, encoded = pair
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        fullscan.analyze_every_frame(tools, source, encoded, 0, None, tmp_path, lambda *_: None,
                                     token, threads=2, source_hwaccel=False)


def test_seeds_follow_libaom():
    # libaom: first frame 7391, then +3381 per shown frame (16-bit), 0 -> 7391
    assert [picture_seed(7391, i) for i in range(3)] == [7391, 10772, 14153]
    seq = [picture_seed(7391, i) for i in range(70000)]
    assert 0 not in seq
    for a, b in zip(seq, seq[1:]):
        assert b == (((a + 3381) & 0xFFFF) or 7391)


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
    assert [y / (1 << later.scaling_shift) for _, y in later.y_points] == \
        pytest.approx([y / (1 << b.scaling_shift) for _, y in b.y_points])
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
