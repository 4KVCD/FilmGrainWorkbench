import numpy as np

from afgs1wb.alignment import check_alignment


def test_alignment_detects_shifted_pairs():
    rng = np.random.default_rng(0)
    frames = rng.normal(500, 60, (6, 40, 40)).astype(np.float32)
    assert check_alignment(frames, frames + rng.normal(0, 2, frames.shape), 1023)[1] == "ok"
    assert check_alignment(frames[1:], frames[:-1], 1023)[1] == "shifted"
    still = np.repeat(frames[:1], 6, axis=0)
    assert check_alignment(still, still, 1023)[1] == "unclear"


def test_frame_match_tells_a_wrong_offset_from_a_still_shot():
    from afgs1wb.alignment import block_means, frame_match
    rng = np.random.default_rng(1)
    # a moving picture: coarse detail that differs from frame to frame, as real pictures have
    scene = [np.kron(rng.normal(500, 120, (8, 8)), np.ones((8, 8))) for _ in range(5)]
    grain = rng.normal(0, 40, (64, 64))                              # heavy grain the encode removed
    means = {k: block_means(scene[k] + grain) for k in range(5)}
    encode = block_means(scene[2])
    assert frame_match(means, encode, 2, 1023)[0] == "same"          # grain alone is not a mismatch
    assert frame_match(means, encode, 1, 1023)[0] == "neighbour"     # off by one
    assert frame_match({k: means[k] for k in (0, 4)} | {1: block_means(scene[0])}, encode, 1, 1023)[0] == "none"
    still = {k: block_means(scene[0] + rng.normal(0, 40, (64, 64))) for k in range(3)}
    assert frame_match(still, block_means(scene[0]), 1, 1023)[0] == "unclear"
    assert frame_match(means, encode, 9, 1023)[0] == "missing"
