from afgs1wb.grain_params import format_grain_table, parse_grain_table

# Verbatim noise_model (libaom) output.
SAMPLE = """filmgrn1
E 0 1584082500 1 7391 1
\tp 3 7 0 11 0 1 128 192 256 128 192 256
\tsY 2  0 6 255 9
\tsCb 6 0 15 13 15 27 24 54 15 108 15 255 16
\tsCr 2 0 7 255 9
\tcY 0 2 -8 11 -1 -2 1 2 -5 12 -25 -1 1 1 -9 8 -23 61 14 9 8 18 -20 75
\tcCb 0 1 1 5 2 -2 3 1 0 12 -23 -11 0 3 -2 12 -60 83 13 4 1 7 -28 99 -1
\tcCr -1 1 2 3 2 -3 2 1 4 5 -16 -12 0 2 -1 4 -55 81 15 5 2 5 -20 98 0
E 1584082500 2112110000 1 0 0
"""


def test_parse_noise_model_output():
    entries = parse_grain_table(SAMPLE)
    assert len(entries) == 2
    p = entries[0].params
    assert p.ar_coeff_lag == 3 and p.ar_coeff_shift == 7 and p.scaling_shift == 11
    assert p.y_points == [(0, 6), (255, 9)]
    assert len(p.cb_points) == 6 and p.cb_points[-1] == (255, 16)
    assert len(p.ar_coeffs_y) == 24 and len(p.ar_coeffs_cb) == 25
    assert p.random_seed == 7391
    # update_parameters == 0 reuses the previous parameters with a new seed.
    assert entries[1].params.y_points == p.y_points and entries[1].params.random_seed == 0


def test_format_roundtrip():
    entries = parse_grain_table(SAMPLE)
    again = parse_grain_table(format_grain_table(entries))
    assert [e.params for e in again] == [e.params for e in entries]
    assert [(e.start, e.end) for e in again] == [(e.start, e.end) for e in entries]


def test_parsing_pauses_the_garbage_collector_and_restores_it():
    import gc
    from afgs1wb.grain_params import TableEntry, bulk_objects, parse_grain_table
    from conftest import sample_params
    assert gc.isenabled()
    parse_grain_table(format_grain_table([TableEntry(0, 10, sample_params())]))
    assert gc.isenabled()                       # back on after parsing
    gc.disable()
    try:
        with bulk_objects():
            pass
        assert not gc.isenabled()               # a caller's own choice is kept
    finally:
        gc.enable()
