import numpy as np

from afgs1wb.alignment import check_alignment


def test_alignment_detects_shifted_pairs():
    rng = np.random.default_rng(0)
    frames = rng.normal(500, 60, (6, 40, 40)).astype(np.float32)
    assert check_alignment(frames, frames + rng.normal(0, 2, frames.shape), 1023)[1] == "ok"
    assert check_alignment(frames[1:], frames[:-1], 1023)[1] == "shifted"
    still = np.repeat(frames[:1], 6, axis=0)
    assert check_alignment(still, still, 1023)[1] == "unclear"
