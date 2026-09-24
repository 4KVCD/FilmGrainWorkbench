import numpy as np
import pytest

from afgs1wb.analysis import (AnalysisSettings, GrainModel, analyze, check_alignment,
                              entry_sigmas, merge_entries, plan_runs)
from afgs1wb.probe import probe
from afgs1wb.tools import CancelToken
from afgs1wb.tuning import Tuning, build_params, fit_points

from conftest import sample_params


class FakeInfo:
    def __init__(self, frames):
        self.frame_count = frames


def test_plan_runs_spreads_runs_and_skips_the_ends():
    starts = plan_runs(FakeInfo(10000), FakeInfo(10000), AnalysisSettings(runs=10, frames_per_run=8))
    assert len(starts) == 10
    assert starts[0] >= 500 and starts[-1] + 8 <= 9500
    assert all(b - a > 900 for a, b in zip(starts, starts[1:]))


def test_plan_runs_respects_offset_and_short_sources():
    starts = plan_runs(FakeInfo(1000), FakeInfo(900), AnalysisSettings(runs=5, frames_per_run=8, offset=50))
    assert all(0 <= s and s + 50 + 8 <= 900 for s in starts)


def test_alignment_detects_shifted_pairs():
    rng = np.random.default_rng(0)
    frames = rng.normal(500, 60, (6, 40, 40)).astype(np.float32)
    assert check_alignment(frames, frames + rng.normal(0, 2, frames.shape), 1023)[1] == "ok"
    assert check_alignment(frames[1:], frames[:-1], 1023)[1] == "shifted"
    still = np.repeat(frames[:1], 6, axis=0)
    assert check_alignment(still, still, 1023)[1] == "unclear"


def test_fit_points_is_accurate_and_sparse():
    curve = np.interp(np.arange(256), [0, 60, 200, 255], [20, 80, 40, 40])
    points = fit_points(curve, 14)
    assert len(points) <= 6
    approx = np.interp(np.arange(256), [x for x, _ in points], [y for _, y in points])
    assert np.max(np.abs(approx - curve)) <= 4      # corners are smoothed over +/-7 bins
    assert np.mean(np.abs(approx - curve)) <= 1.0
    assert fit_points(np.full(256, 30.2), 14) == [(0, 30), (255, 30)]


def model_from(params) -> GrainModel:
    base, y, cb, cr = merge_entries([(params, 1.0)], 10, 1, 1)
    return GrainModel(base, 10, 1, 1, y, cb, cr)


def test_merge_of_identical_entries_is_that_entry():
    p = sample_params()
    base, y, cb, cr = merge_entries([(p, 1.0), (p.copy(), 2.0)], 10, 1, 1)
    ey, ecb, ecr = entry_sigmas(p, 10, 1, 1)
    assert np.allclose(y, ey) and np.allclose(cb, ecb) and np.allclose(cr, ecr)
    assert base.ar_coeffs_y == p.ar_coeffs_y


@pytest.mark.parametrize("strength", [0.5, 1.0, 2.0, 4.0])
def test_strength_scales_output_sigma(strength):
    model = model_from(sample_params())
    built = build_params(model, Tuning(luma_strength=strength, chroma_strength=strength))
    y, cb, _ = entry_sigmas(built.params, 10, 1, 1)
    assert np.allclose(y, model.luma_sigma * strength, rtol=0.06, atol=0.3)
    assert np.allclose(cb, model.cb_sigma * strength, rtol=0.08, atol=0.3)


def test_zero_chroma_strength_disables_chroma_grain():
    built = build_params(model_from(sample_params()), Tuning(chroma_strength=0.0))
    assert built.params.cb_points == [] and built.params.cr_points == []
    assert built.params.y_points


def test_model_json_roundtrip():
    model = model_from(sample_params())
    again = GrainModel.from_json(model.to_json())
    assert np.allclose(again.luma_sigma, model.luma_sigma, atol=1e-3)
    assert again.base.ar_coeffs_cb == model.base.ar_coeffs_cb


def test_analyze_small_clip(tools, need, grainy_source, grainy_clip):
    need("ffmpeg", "ffprobe", "noise_model")
    source = probe(grainy_source, tools.ffprobe)
    encoded = probe(grainy_clip, tools.ffprobe)
    progress = []
    model = analyze(tools, source, encoded, AnalysisSettings(runs=3, frames_per_run=6),
                    lambda f, m: progress.append(f), CancelToken())
    assert [r.alignment for r in model.runs].count("shifted") == 0
    assert all(r.psnr > 30 for r in model.runs)
    assert model.luma_sigma.max() > 1.0          # the clip has strong added noise
    assert progress[-1] == pytest.approx(1.0)


def test_analyze_can_be_cancelled(tools, need, grainy_source, grainy_clip):
    need("ffmpeg", "ffprobe", "noise_model")
    from afgs1wb.tools import Cancelled
    source = probe(grainy_source, tools.ffprobe)
    encoded = probe(grainy_clip, tools.ffprobe)
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        analyze(tools, source, encoded, AnalysisSettings(runs=2, frames_per_run=4),
                lambda *_: None, token)
