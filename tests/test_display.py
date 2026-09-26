"""The on-screen conversion: converting in row bands on threads must not change a single pixel."""

import numpy as np
import pytest

from afgs1wb import display


@pytest.mark.parametrize("transfer,primaries", [("smpte2084", "bt2020"), ("bt709", "bt709")])
@pytest.mark.parametrize("height,subsampling_y", [(2160, 1), (1083, 1), (1608, 0)])
def test_banded_conversion_is_identical_to_one_pass(transfer, primaries, height, subsampling_y):
    width = 3840 if height != 1083 else 1920
    fmt = display.DisplayFormat(10, True, "bt2020nc" if primaries == "bt2020" else "bt709",
                                transfer, primaries, width, height)
    conv = display.Converter(fmt)
    rng = np.random.default_rng(height)
    y = rng.integers(0, 1024, (height, width), dtype=np.uint16)
    ch = (height + subsampling_y) >> subsampling_y
    u = rng.integers(0, 1024, (ch, width // 2), dtype=np.uint16)
    v = rng.integers(0, 1024, (ch, width // 2), dtype=np.uint16)
    banded = conv.to_rgb(y, u, v)
    one_pass = conv._convert(y, u, v, 1 + subsampling_y, 2)
    assert banded.shape == (height, width, 3)
    assert np.array_equal(banded, one_pass)
