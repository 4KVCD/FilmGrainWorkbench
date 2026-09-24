import pytest

from afgs1wb.afgs1 import (PictureFormat, T35_PREFIX, T35Template, decode_t35, encode_t35,
                           picture_seed)
from afgs1wb.grain_params import GrainParams

from conftest import sample_params

FMT = PictureFormat(3840, 2160, 10, 1, 1)


@pytest.mark.parametrize("lag", [0, 1, 2, 3])
def test_roundtrip_all_lags(lag):
    p = sample_params(lag)
    decoded = decode_t35(encode_t35(p, FMT, 4321))
    assert len(decoded) == 1
    d = decoded[0]
    assert d.seed == 4321
    assert d.format == FMT
    for name in ("y_points", "cb_points", "cr_points", "scaling_shift", "ar_coeff_lag",
                 "ar_coeffs_y", "ar_coeffs_cb", "ar_coeffs_cr", "ar_coeff_shift",
                 "grain_scale_shift", "cb_mult", "cb_luma_mult", "cb_offset", "cr_mult",
                 "cr_luma_mult", "cr_offset", "overlap_flag", "clip_to_restricted_range"):
        assert getattr(d.params, name) == getattr(p, name), name


def test_payload_starts_with_aom_t35_header():
    payload = encode_t35(sample_params(), FMT, 1)
    assert payload.startswith(T35_PREFIX) and payload[:4] == b"\xb5\x58\x90\x01"
    assert payload[4] >> 7 == 1          # afgs1_enable_flag


def test_empty_chroma_is_coded_as_silent_point():
    # FFmpeg misparses num_cb_points == 0, so "no chroma grain" must be one zero point.
    p = sample_params(chroma=False)
    d = decode_t35(encode_t35(p, FMT, 9))[0].params
    assert d.cb_points == [(0, 0)] and d.cr_points == [(0, 0)]


def test_chroma_scaling_from_luma_roundtrip():
    p = sample_params()
    p.chroma_scaling_from_luma = True
    d = decode_t35(encode_t35(p, FMT, 9))[0].params
    assert d.chroma_scaling_from_luma and d.cb_points == [] and d.cr_points == []
    assert d.ar_coeffs_cb == p.ar_coeffs_cb


def test_template_matches_full_encode():
    p = sample_params()
    template = T35Template(p, FMT)
    for seed in (1, 2, 0x8000, 0xFFFF, 12345):
        assert template.for_seed(seed) == encode_t35(p, FMT, seed)


def test_picture_seed_is_never_zero():
    seeds = {picture_seed(base, i) for base in (0, 1, 65535, 7391) for i in range(5000)}
    assert 0 not in seeds and max(seeds) <= 0xFFFF


def test_8k_uses_resolution_units():
    fmt = PictureFormat(7680, 4320, 10)
    assert decode_t35(encode_t35(sample_params(), fmt, 1))[0].format == fmt


def test_rejects_invalid_points():
    p = sample_params()
    p.y_points = [(10, 5), (10, 6)]
    with pytest.raises(ValueError):
        encode_t35(p, FMT, 1)


def test_apply_grain_off():
    d = decode_t35(encode_t35(GrainParams(apply_grain=False), FMT, 5))[0]
    assert not d.params.apply_grain
