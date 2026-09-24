from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from afgs1wb.grain_params import GrainParams
from afgs1wb.tools import detect_tools

CACHE = Path(__file__).parent / ".cache"


@pytest.fixture(scope="session")
def tools():
    return detect_tools()


def _need(tools, *names):
    missing = tools.missing(*names)
    if missing:
        pytest.skip("needs " + ", ".join(missing))


@pytest.fixture(scope="session")
def grainy_clip(tools) -> Path:
    """49 frames of noisy 320x184 test pattern as 10-bit VVC with audio and subtitles."""
    _need(tools, "ffmpeg", "ffprobe")
    CACHE.mkdir(exist_ok=True)
    clip = CACHE / "grainy_vvc.mkv"
    if not clip.exists():
        srt = CACHE / "sub.srt"
        srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nhello\n", encoding="utf-8")
        result = subprocess.run(
            [tools.ffmpeg, "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x184:rate=24:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-i", str(srt),
             "-vf", "noise=c0s=12:c0f=t+u,format=yuv420p", "-map", "0", "-map", "1", "-map", "2",
             "-c:v", "libvvenc", "-preset", "faster", "-qp", "37", "-c:a", "aac", "-c:s", "srt",
             str(clip)], capture_output=True, text=True)
        if result.returncode:
            pytest.skip("ffmpeg cannot encode VVC here: " + result.stderr[-300:])
    return clip


@pytest.fixture(scope="session")
def grainy_source(tools) -> Path:
    """The lossless grainy source the clip was encoded from (same frames)."""
    _need(tools, "ffmpeg")
    CACHE.mkdir(exist_ok=True)
    src = CACHE / "grainy_source.mkv"
    if not src.exists():
        subprocess.run(
            [tools.ffmpeg, "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=320x184:rate=24:duration=2",
             "-vf", "noise=c0s=12:c0f=t+u,format=yuv420p", "-c:v", "ffv1", str(src)], check=True)
    return src


def sample_params(lag: int = 3, chroma: bool = True) -> GrainParams:
    n = 2 * lag * (lag + 1)
    coeffs = [((i * 7) % 23) - 11 for i in range(n)]
    return GrainParams(
        y_points=[(0, 40), (128, 90), (255, 60)],
        cb_points=[(0, 20), (255, 30)] if chroma else [],
        cr_points=[(0, 25), (200, 35)] if chroma else [],
        scaling_shift=10, ar_coeff_lag=lag, ar_coeff_shift=7,
        ar_coeffs_y=coeffs, ar_coeffs_cb=[c // 2 for c in coeffs] + [9],
        ar_coeffs_cr=[-c // 2 for c in coeffs] + [-6],
        overlap_flag=True, clip_to_restricted_range=True)


@pytest.fixture
def need(tools):
    return lambda *names: _need(tools, *names)
