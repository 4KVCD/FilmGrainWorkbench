"""Run the C++ engine (fgw_check) and the Python app on the same real files, and compare.

    .venv\\Scripts\\python tests\\tools\\compare_with_python.py [--fast] SOURCE ENCODED

SOURCE and ENCODED are a real film and a VVC encode of it (4K is fine). The
small synthetic pair the Python tests make (tests/.cache) is used for the
slow steps (the every-frame analysis) and for writing. Prints one line per
check and exits 1 if any differs. A developer tool for the C++ port.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from afgs1wb import alignment, frames, fullscan, grain_params, mux, probe, vvcgrain  # noqa: E402
from afgs1wb.tools import CancelToken, detect_tools  # noqa: E402

CHECK = ROOT / "build" / "cmake" / "tests" / "Release" / "fgw_check.exe"
TOOLS = detect_tools()
failures: list[str] = []


def cpp(*args) -> object:
    started = time.perf_counter()
    out = subprocess.run([str(CHECK), *map(str, args)], capture_output=True, text=True, encoding="utf-8")
    result = json.loads(out.stdout)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"fgw_check {args[0]}: {result['error']}")
    cpp.seconds = time.perf_counter() - started
    return result


def report(name: str, same: bool, detail: str = "") -> None:
    print(f"{'same' if same else 'DIFFERENT':9s} {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not same:
        failures.append(name)


def info_dict(info) -> dict:
    return {"stream_index": info.stream_index, "codec": info.codec, "width": info.width, "height": info.height,
            "fps": [info.fps.numerator, info.fps.denominator], "pix_fmt": info.pix_fmt,
            "bit_depth": info.bit_depth, "subsampling": [info.subsampling_x, info.subsampling_y],
            "duration": info.duration, "first_pts": info.first_pts, "color_range": info.color_range,
            "matrix": info.matrix, "transfer": info.transfer, "primaries": info.primaries,
            "start_time": info.start_time, "frames": info.frames, "frame_count": info.frame_count,
            "describe": info.describe()}


def check_probe(path: Path):
    py = info_dict(probe.probe(path, TOOLS.ffprobe))
    c = cpp("probe", path)
    report(f"probe {path.name}", py == c, "" if py == c else f"{py} vs {c}")
    return probe.probe(path, TOOLS.ffprobe)


def check_frames(info, start, count, target=None, crop=None, label=""):
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "f.raw"
        extra = []
        if target:
            extra = [target.width, target.height, target.bit_depth, target.subsampling_x, target.subsampling_y]
            if crop:
                extra += [crop.width, crop.height, crop.x, crop.y]
        c = cpp("frames", info.path, start, count, raw, *extra)
        data = np.fromfile(raw, dtype="<u2")
    py = frames.read_frames(TOOLS.ffmpeg, info, start, count, None, target, crop)
    same_index = [f.index for f in py] == c["indexes"]
    py_data = np.concatenate([np.concatenate([f.y.ravel(), f.u.ravel(), f.v.ravel()]).astype("<u2") for f in py])
    same = same_index and np.array_equal(py_data, data)
    report(f"read_frames {label} {info.path.name} {start}+{count}", same,
           f"indexes {[f.index for f in py]}, {py_data.size:,} samples")


def check_simple(name, py, c):
    report(name, py == c, "" if py == c else f"python {py} vs c++ {c}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("encoded", type=Path)
    parser.add_argument("--index", type=int, default=50000)
    args = parser.parse_args()

    source = check_probe(args.source)
    encoded = check_probe(args.encoded)
    for small in (ROOT / "tests" / ".cache").glob("*.mkv"):
        check_probe(small)
    crop = frames.source_crop_for(source, encoded)

    check_frames(encoded, args.index, 4, label="encode")
    check_frames(source, args.index, 2, frames.Geometry.of(encoded), crop, label="source cropped")
    check_frames(source, 1000, 1, frames.Geometry(960, 400, 10, 1, 1), None, label="source scaled")

    bars = vvcgrain.detect_black_bars(TOOLS.ffmpeg, source)
    check_simple("detect_black_bars", None if bars is None else [bars.width, bars.height, bars.x, bars.y],
                 cpp("blackbars", source.path))

    py_offset = alignment.detect_offset(TOOLS, source, encoded, crop)
    c_offset = cpp("offset", source.path, encoded.path)
    report("detect_offset", py_offset[0] == c_offset["offset"] and abs(py_offset[1] - c_offset["confidence"]) < 1e-6,
           f"python {py_offset} c++ {c_offset}")

    enc_frame = frames.read_frame(TOOLS.ffmpeg, encoded, args.index)
    src_frame = frames.read_frame(TOOLS.ffmpeg, source, args.index, None, frames.Geometry.of(encoded), crop)
    py_table = grain_params.format_grain_table(
        [grain_params.TableEntry(0, 1, fullscan.estimate_frame(src_frame, enc_frame))])
    check_simple("estimate_frame", py_table, cpp("estimate", source.path, encoded.path, args.index, 0)["table"])

    grainy, grainy_source = ROOT / "tests/.cache/grainy_vvc.mkv", ROOT / "tests/.cache/grainy_source.mkv"
    if grainy.exists() and grainy_source.exists():
        gi, gs = probe.probe(grainy, TOOLS.ffprobe), probe.probe(grainy_source, TOOLS.ffprobe)
        with tempfile.TemporaryDirectory() as py_dir, tempfile.TemporaryDirectory() as c_dir:
            model = fullscan.analyze_every_frame(TOOLS, gs, gi, 0, None, Path(py_dir), lambda f, m: None,
                                                 CancelToken())
            py_table = grain_params.format_grain_table(
                [grain_params.TableEntry(s.start, s.end, s.params) for s in model.segments])
            c = cpp("analyze", gs.path, gi.path, 0, c_dir)
            report("analyze_every_frame (table)", py_table == c["table"], f"{len(model.segments)} segments")
            check_simple("analyze_every_frame (alignment, warnings)", [model.alignment, model.notes["warnings"]],
                         [c["alignment"], c["warnings"]])
            report("analyze_every_frame (sigmas)", model.sigma_frames == c["sigma_frames"] and
                   np.allclose(model.sigma_values, c["sigma_values"], atol=1e-9))

        table = Path(tempfile.gettempdir()) / "fgw_compare.tbl"
        table.write_text(grain_params.format_grain_table(
            [grain_params.TableEntry(0, 1, model.segments[0].params)]), encoding="ascii")
        with tempfile.TemporaryDirectory() as tmp:
            py_out, c_out = Path(tmp) / "py.mkv", Path(tmp) / "c.mkv"
            py_report = mux.write_afgs1_mkv(TOOLS, gi, py_out, model.segments[0].params, 7391,
                                            lambda f, m: None, CancelToken())
            c = cpp("write", gi.path, c_out, table, 7391)
            check_simple("write_afgs1_mkv (report)", [py_report.pictures, py_report.packets, py_report.notes,
                                                     py_report.copied],
                         [c["pictures"], c["packets"], c["notes"], c["copied"]])

            def packets(path: Path) -> str:
                return subprocess.run([TOOLS.ffmpeg, "-v", "error", "-i", str(path), "-map", "0", "-c", "copy",
                                       "-f", "framemd5", "-"], capture_output=True, text=True).stdout
            report("write_afgs1_mkv (every packet of every track, with timestamps)", packets(py_out) == packets(c_out))
        table.unlink()

    with tempfile.TemporaryDirectory() as py_dir, tempfile.TemporaryDirectory() as c_dir:
        bars = vvcgrain.detect_black_bars(TOOLS.ffmpeg, source)
        cut = vvcgrain.cut_scene(TOOLS.ffmpeg, source, args.index, 33, bars, Path(py_dir), CancelToken(),
                                 lambda f, m: None)
        settings = vvcgrain.VvcSettings(fga=2, strength=100, qp=28.0, preset="faster", frames=33)
        clip = vvcgrain.encode_scene(vvcgrain.find_vvc_tools(), cut, settings, CancelToken(), lambda f, m: None)
        c = cpp("scene", source.path, args.index, 33, 2, 100, 28, "faster", c_dir)
        check_simple("cut_scene (start, count, hdr)", [cut.start, cut.count, cut.hdr], [c["start"], c["count"], c["hdr"]])
        same_files = all((Path(clip.folder) / n).read_bytes() == (Path(c["folder"]) / n).read_bytes()
                         for n in ("clip.266", "decoded.yuv", "grain.yuv"))
        same_source = (Path(py_dir) / "source.yuv").read_bytes() == (Path(c_dir) / "source.yuv").read_bytes()
        report("cut_scene + encode_scene (source.yuv, clip.266, decoded.yuv, grain.yuv)", same_files and same_source)
        check_simple("read_grain (describe of every picture)", [g.describe() if g else None for g in clip.grain],
                     c["grain"])
        stats = vvcgrain.frame_stats(clip.frame("source", clip.start + 1), clip.frame("decoded", clip.start + 1),
                                     clip.frame("grain", clip.start + 1))
        py_stats = [list(stats.removed), list(stats.added), list(stats.shift)]
        c_stats = [c["stats"]["removed"], c["stats"]["added"], c["stats"]["shift"]]
        report("frame_stats (to 1e-4)", np.allclose(py_stats, c_stats, atol=1e-4), f"{py_stats} vs {c_stats}")

    print(f"\n{len(failures)} different" + (": " + ", ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
