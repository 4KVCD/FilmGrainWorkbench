import numpy as np

from afgs1wb import synth
from afgs1wb.grain_params import GrainParams

from conftest import sample_params


def test_scaling_lut_follows_points_and_extends_flat():
    lut = synth.scaling_lut([(16, 10), (32, 26), (200, 26)])
    assert lut[0] == 10 and lut[16] == 10 and lut[24] == 18 and lut[32] == 26
    assert lut[255] == 26


def test_no_points_means_no_change():
    y = np.full((64, 64), 512, np.uint16)
    u = np.full((32, 32), 512, np.uint16)
    out = synth.apply_grain(y, u, u.copy(), GrainParams(), 5, 10)
    assert np.array_equal(out[0], y) and np.array_equal(out[1], u)


def test_same_seed_same_grain_different_seed_different_grain():
    y = np.full((96, 128), 400, np.uint16)
    u = np.full((48, 64), 512, np.uint16)
    p = sample_params()
    a = synth.apply_grain(y, u, u, p, 11, 10)[0]
    b = synth.apply_grain(y, u, u, p, 11, 10)[0]
    c = synth.apply_grain(y, u, u, p, 12, 10)[0]
    assert np.array_equal(a, b) and not np.array_equal(a, c)


def test_restricted_range_clipping():
    y = np.full((64, 64), 940, np.uint16)
    u = np.full((32, 32), 512, np.uint16)
    p = sample_params()
    p.y_points = [(0, 255), (255, 255)]
    out = synth.apply_grain(y, u, u, p, 3, 10)[0]
    assert out.max() <= 940 and out.min() >= 64


def test_template_std_grows_with_scale_shift_removed():
    p = sample_params()
    loud = synth.luma_template_std(p, 10)
    p.grain_scale_shift = 1
    assert 0.4 < synth.luma_template_std(p, 10) / loud < 0.6
