"""Write the reference results the C++ tests compare with, from the Python app's own functions.

    .venv\\Scripts\\python tests\\reference\\make_reference.py

Inputs are fixed and made the same way in C++ (tests/core/reference.h):
synthetic frames from synthetic_plane(), grain parameter sets from
param_cases(). Large outputs (planes, RGB pictures, files) are stored as
SHA-256 digests of their bytes (planes as little-endian uint16, RGB as R, G, B
bytes).
"""

from __future__ import annotations

from pathlib import Path
from fractions import Fraction
import hashlib
import json
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from afgs1wb import afgs1, display, grain_params, synth, tuning, vvc  # noqa: E402
from afgs1wb.grain_params import GrainParams  # noqa: E402
from afgs1wb.mkvwriter import MatroskaVideoWriter  # noqa: E402

OUT = Path(__file__).resolve().parent


def synthetic_plane(width: int, height: int, bit_depth: int, salt: int) -> np.ndarray:
    """A deterministic plane with every kind of sample value, made the same way in C++."""
    peak = (1 << bit_depth) - 1
    r = np.arange(height, dtype=np.int64)[:, None]
    c = np.arange(width, dtype=np.int64)[None, :]
    values = (r * 37 + c * 11 + (r * c + salt) % 13 * salt + salt * 101) % (peak + 1)
    return values.astype(np.uint16)


def synthetic_frame(width, height, bit_depth, subx, suby):
    cw, ch = (width + subx) >> subx, (height + suby) >> suby
    return (synthetic_plane(width, height, bit_depth, 1), synthetic_plane(cw, ch, bit_depth, 2),
            synthetic_plane(cw, ch, bit_depth, 3))


def digest(*arrays) -> str:
    h = hashlib.sha256()
    for a in arrays:
        h.update(np.ascontiguousarray(a).astype("<u2" if a.dtype != np.uint8 else np.uint8).tobytes())
    return h.hexdigest()


def params_json(p: GrainParams) -> dict:
    return {"apply_grain": p.apply_grain, "random_seed": p.random_seed,
            "y_points": [list(t) for t in p.y_points], "cb_points": [list(t) for t in p.cb_points],
            "cr_points": [list(t) for t in p.cr_points], "chroma_scaling_from_luma": p.chroma_scaling_from_luma,
            "scaling_shift": p.scaling_shift, "ar_coeff_lag": p.ar_coeff_lag,
            "ar_coeffs_y": list(p.ar_coeffs_y), "ar_coeffs_cb": list(p.ar_coeffs_cb),
            "ar_coeffs_cr": list(p.ar_coeffs_cr), "ar_coeff_shift": p.ar_coeff_shift,
            "grain_scale_shift": p.grain_scale_shift, "cb_mult": p.cb_mult, "cb_luma_mult": p.cb_luma_mult,
            "cb_offset": p.cb_offset, "cr_mult": p.cr_mult, "cr_luma_mult": p.cr_luma_mult,
            "cr_offset": p.cr_offset, "overlap_flag": p.overlap_flag,
            "clip_to_restricted_range": p.clip_to_restricted_range}


def param_cases() -> list[GrainParams]:
    """Grain parameter sets covering lags, chroma modes, shifts and flags."""
    cases = []
    for lag in range(4):
        n = 2 * lag * (lag + 1)
        cases.append(GrainParams(
            random_seed=1000 + lag, y_points=[(0, 20), (64, 45), (128, 80), (255, 40)],
            cb_points=[(0, 10), (255, 30)], cr_points=[(16, 5), (100, 40), (240, 25)],
            scaling_shift=8 + lag % 4, ar_coeff_lag=lag,
            ar_coeffs_y=[((k * 37) % 61) - 30 for k in range(n)],
            ar_coeffs_cb=[((k * 23) % 41) - 20 for k in range(n + 1)],
            ar_coeffs_cr=[((k * 29) % 51) - 25 for k in range(n + 1)],
            ar_coeff_shift=6 + lag % 4, grain_scale_shift=lag % 2, overlap_flag=lag % 2 == 0,
            clip_to_restricted_range=lag == 3, cb_mult=128 + lag, cb_luma_mult=192 - lag * 3,
            cb_offset=256 + lag * 5, cr_mult=120, cr_luma_mult=200, cr_offset=250))
    cases.append(GrainParams(random_seed=7391, y_points=[(0, 0), (255, 60)], chroma_scaling_from_luma=True,
                             ar_coeff_lag=1, ar_coeffs_y=[4, -3, 2, 9], ar_coeffs_cb=[1, 2, 3, 4, 5],
                             ar_coeffs_cr=[-1, -2, -3, -4, -5], ar_coeff_shift=7))
    cases.append(GrainParams(random_seed=12, y_points=[], cb_points=[(0, 30), (255, 30)],
                             cr_points=[(0, 20), (255, 50)], ar_coeff_lag=2,
                             ar_coeffs_cb=[3] * 12, ar_coeffs_cr=[-2] * 12, scaling_shift=10))
    cases.append(GrainParams(random_seed=99, y_points=[(30, 255), (200, 255)], scaling_shift=11,
                             ar_coeff_lag=0, clip_to_restricted_range=True, overlap_flag=False))
    cases.append(GrainParams(apply_grain=False))
    return cases


FRAME_SIZES = [(64, 48, 10, 1, 1), (97, 61, 10, 1, 1), (130, 90, 8, 1, 1), (70, 40, 10, 1, 0),
               (50, 34, 12, 0, 0), (200, 120, 10, 1, 1)]


def synth_cases() -> dict:
    out = {"templates": [], "luts": [], "grain": [], "template_std": []}
    for i, p in enumerate(param_cases()):
        for bit_depth, subx, suby in ((10, 1, 1), (8, 1, 0), (12, 0, 0)):
            t = synth.generate_grain(p, p.random_seed, bit_depth, subx, suby)
            out["templates"].append({"params": i, "bit_depth": bit_depth, "subx": subx, "suby": suby,
                                     "luma": digest(t.luma.astype(np.int32).astype("<i4").view(np.uint16)),
                                     "cb": digest(t.cb.astype(np.int32).astype("<i4").view(np.uint16)),
                                     "cr": digest(t.cr.astype(np.int32).astype("<i4").view(np.uint16))})
        for points in (p.y_points, p.cb_points, p.cr_points):
            out["luts"].append({"points": [list(t) for t in points], "lut": synth.scaling_lut(points).tolist()})
        if p.apply_grain:
            out["template_std"].append({"params": i, "bit_depth": 10,
                                        "luma": synth.luma_template_std(p, 10) if p.y_points else 0.0,
                                        "chroma_420": list(synth.chroma_template_std(p, 10, 1, 1))})
    for i, p in enumerate(param_cases()):
        for width, height, bit_depth, subx, suby in FRAME_SIZES:
            y, u, v = synthetic_frame(width, height, bit_depth, subx, suby)
            seed = afgs1.picture_seed(p.random_seed, i * 7 + width)
            gy, gu, gv = synth.apply_grain(y, u, v, p, seed, bit_depth, subx, suby)
            out["grain"].append({"params": i, "width": width, "height": height, "bit_depth": bit_depth,
                                 "subx": subx, "suby": suby, "seed": seed, "result": digest(gy, gu, gv)})
    return out


def afgs1_cases() -> dict:
    out = {"encode": [], "seeds": []}
    formats = [afgs1.PictureFormat(3840, 2160, 10), afgs1.PictureFormat(1920, 1080, 8),
               afgs1.PictureFormat(3840, 1608, 10, 1, 0), afgs1.PictureFormat(1280, 720, 12, 0, 0)]
    for i, p in enumerate(param_cases()):
        for f, fmt in enumerate(formats):
            sub420 = (fmt.subsampling_x, fmt.subsampling_y) == (1, 1)
            try:
                p.validate(sub420)
            except ValueError:
                continue
            payload = afgs1.encode_t35(p, fmt, 4321 + i)
            template = afgs1.T35Template(p, fmt)
            decoded = afgs1.decode_t35(payload)
            out["encode"].append({"params": i, "format": f, "seed": 4321 + i, "payload": payload.hex(),
                                  "template_seed_777": template.for_seed(777).hex(),
                                  "decoded": [{"params": params_json(d.params), "seed": d.seed,
                                               "format": [d.format.width, d.format.height, d.format.bit_depth,
                                                          d.format.subsampling_x, d.format.subsampling_y,
                                                          d.format.monochrome]} for d in decoded]})
    out["formats"] = [[f.width, f.height, f.bit_depth, f.subsampling_x, f.subsampling_y] for f in formats]
    for base in (0, 7391, 65535, 123):
        out["seeds"].append({"base": base, "indexes": [0, 1, 2, 19, 20, 1000, 150000],
                             "values": [afgs1.picture_seed(base, k) for k in (0, 1, 2, 19, 20, 1000, 150000)]})
    return out


def as_libaom_writes(p: GrainParams) -> GrainParams:
    """Every luma coefficient written, zeros without luma grain, as libaom's tables have them."""
    n = grain_params.num_luma_ar_coeffs(p.ar_coeff_lag)
    return p if len(p.ar_coeffs_y) == n else p.copy(ar_coeffs_y=[0] * n)


def table_cases() -> dict:
    entries = [grain_params.TableEntry(k * 1000, (k + 1) * 1000, as_libaom_writes(p))
               for k, p in enumerate(param_cases())]
    text = grain_params.format_grain_table(entries)
    parsed = grain_params.parse_grain_table(text)
    return {"text": text, "parsed": [{"start": e.start, "end": e.end, "params": params_json(e.params)}
                                     for e in parsed]}


def tuning_cases() -> dict:
    out = {"scaled": [], "sigmas": []}
    for i, p in enumerate(param_cases()):
        for strengths in ((1.0, 1.0), (2.0, 0.5), (0.0, 1.5), (4.0, 4.0), (0.37, 2.6)):
            t = tuning.Tuning(*strengths, overlap=i % 2 == 0, clip_to_restricted_range=i == 2, seed=555 + i)
            for sub420 in (True, False):
                scaled, clipped = tuning.scale_params(p, t, sub420)
                out["scaled"].append({"params": i, "luma": strengths[0], "chroma": strengths[1],
                                      "overlap": t.overlap, "clip": t.clip_to_restricted_range, "seed": t.seed,
                                      "subsampled_420": sub420, "result": params_json(scaled),
                                      "clipped": clipped})
        if p.apply_grain:
            y, cb, cr = tuning.entry_sigmas(p, 10, 1, 1)
            out["sigmas"].append({"params": i, "y": y.tolist(), "cb": cb.tolist(), "cr": cr.tolist()})
    return out


def display_cases() -> dict:
    out = []
    formats = [("bt709", "bt709", "bt709", True), ("bt2020nc", "smpte2084", "bt2020", True),
               ("bt2020nc", "arib-std-b67", "bt2020", True), ("smpte170m", "", "", False),
               ("", "bt709", "bt709", True), ("bt2020nc", "smpte2084", "bt709", False)]
    for width, height, bit_depth, subx, suby in FRAME_SIZES:
        y, u, v = synthetic_frame(width, height, bit_depth, subx, suby)
        for matrix, transfer, primaries, limited in formats:
            fmt = display.DisplayFormat(bit_depth, limited, matrix, transfer, primaries, width, height)
            rgb = display.Converter(fmt).to_rgb(y, u, v)
            out.append({"width": width, "height": height, "bit_depth": bit_depth, "subx": subx, "suby": suby,
                        "matrix": matrix, "transfer": transfer, "primaries": primaries, "limited": limited,
                        "rgb": digest(rgb)})
        grained = synthetic_plane(width, height, bit_depth, 7)
        out.append({"width": width, "height": height, "bit_depth": bit_depth, "grain_only":
                    digest(display.grain_only(y, grained, bit_depth)[:, :, 0])})
    return {"cases": out}


def vvc_cases() -> dict:
    """Packets made of synthetic NAL units: parameter sets, SEIs (with and without AFGS1), PH, slices."""
    def nal(kind: int, body: bytes, layer: int = 0, tid1: int = 1) -> bytes:
        return bytes((layer, (kind << 3) | tid1)) + body

    old_afgs1 = afgs1.encode_t35(param_cases()[0], afgs1.PictureFormat(64, 48, 10), 5)
    other_sei = vvc.sei_nal(137, bytes(range(24)), 0, 1)
    mixed_sei = bytes((0, (23 << 3) | 1)) + vvc.escape_rbsp(
        bytes((4, len(old_afgs1))) + old_afgs1 + bytes((5, 3, 0, 0, 1)) + b"\x80")
    packets = [
        [nal(14, b"\x01\x02"), nal(15, b"\x00\x00\x00\x01\x00\x00\x02"), nal(1, b"\x10\x20\x30")],
        [nal(20, b"\x55"), other_sei, mixed_sei, nal(19, b"\x80\x00"), nal(0, b"\x11" * 40, 2, 3)],
        [nal(16, b"\x00\x00\x00"), vvc.sei_nal(4, old_afgs1, 0, 1), nal(8, b"\x00\x00\x03\x01" * 9)],
        [nal(17, b"\x01"), nal(18, b"\x02")],
    ]
    payload = afgs1.encode_t35(param_cases()[1], afgs1.PictureFormat(3840, 2160, 10), 99)
    out = {"payload": payload.hex(), "packets": []}
    for length_size in (1, 2, 4):
        for nals in packets:
            packet = vvc.join_nals(nals, length_size)
            inserted, is_picture = vvc.insert_t35(packet, length_size, payload)
            out["packets"].append({"length_size": length_size, "packet": packet.hex(),
                                   "inserted": inserted.hex(), "is_picture": is_picture,
                                   "sei_types": sorted(vvc.prefix_sei_types(packet, length_size)),
                                   "t35": [p.hex() for p in vvc.find_t35(inserted, length_size)]})
    samples = [b"", b"\x00\x00\x00\x00", b"\x00\x00\x01\x00\x00\x02\x00\x00\x03\x00\x00\x04",
               bytes(range(256)) * 2, b"\x00" * 9 + b"\x03"]
    out["escape"] = [{"rbsp": s.hex(), "escaped": vvc.escape_rbsp(s).hex(),
                      "unescaped": vvc.unescape_rbsp(vvc.escape_rbsp(s)).hex()} for s in samples]
    return out


def mkv_cases() -> dict:
    path = OUT / "_mkv_case.mkv"
    w = MatroskaVideoWriter(path, bytes((0xFF, 0xF8, 1, 2, 3)), 1920, 1080, 41708333)
    for k in range(120):
        data = bytes((k * 7 + j) % 256 for j in range(50 + k))
        w.write(data, k * 42 + (31000 if k > 60 else 0), k % 24 == 0, 42)
    w.close()
    digest_hex = hashlib.sha256(path.read_bytes()).hexdigest()
    size = path.stat().st_size
    path.unlink()
    return {"sha256": digest_hex, "size": size}


PROBE_VIDEOS = {
    # name: ffmpeg arguments after the inputs (lavfi testsrc2 + sine)
    "h264_ntsc.mkv": ["-r", "24000/1001", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "hevc_pq.mp4": ["-r", "25", "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020",
                    "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-x265-params", "log-level=none",
                    "-c:a", "aac"],
    "ffv1_444_full.mkv": ["-r", "30", "-c:v", "ffv1", "-pix_fmt", "yuv444p10le", "-color_range", "pc", "-an"],
    "vvc_hlg.mkv": ["-r", "50", "-c:v", "libvvenc", "-preset", "faster", "-qp", "40", "-pix_fmt", "yuv420p10le",
                    "-color_trc", "arib-std-b67", "-color_primaries", "bt709", "-colorspace", "bt709", "-an"],
}


def probe_cases() -> dict:
    """ffprobe's answers for small synthetic videos (re-muxed by mkvmerge for its statistics tags),
    and the Python app's VideoInfo, filter chains and commands from them."""
    import subprocess
    from afgs1wb import frames, probe as probe_module
    from afgs1wb.tools import detect_tools
    tools = detect_tools()
    cache = ROOT / "tests" / ".cache" / "probe"
    cache.mkdir(parents=True, exist_ok=True)
    out = []
    for name, args in PROBE_VIDEOS.items():
        made = cache / name
        if not made.exists():
            subprocess.run([tools.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=320x184:rate=24:duration=2", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=2", *args, str(made)], check=True)
        videos = [made]
        if made.suffix == ".mkv":
            remuxed = cache / ("mkvmerge_" + name)
            if not remuxed.exists():
                subprocess.run([tools.mkvmerge, "--quiet", "-o", str(remuxed), str(made)], check=True)
            videos.append(remuxed)
        for video in videos:
            video = video.relative_to(ROOT)          # paths in the reference without this PC's folders
            answers = {}
            real_run_text = probe_module.run_text

            def recording(command, cancel=None):
                text = real_run_text(command, cancel)
                answers["packets" if "packet=pts_time" in command else "streams"] = text
                return text
            probe_module.run_text = recording
            try:
                info = probe_module.probe(video, tools.ffprobe)
            finally:
                probe_module.run_text = real_run_text
            target = frames.Geometry(160, 92, 10, 1, 1)
            crop = frames.Crop(300, 160, 10, 12)
            out.append({
                "name": video.name, "path": str(info.path), "streams": answers["streams"],
                "packets": answers["packets"],
                "info": {"stream_index": info.stream_index, "codec": info.codec, "width": info.width,
                         "height": info.height, "fps": [info.fps.numerator, info.fps.denominator],
                         "pix_fmt": info.pix_fmt, "bit_depth": info.bit_depth,
                         "subsampling": [info.subsampling_x, info.subsampling_y], "duration": info.duration,
                         "first_pts": info.first_pts, "color_range": info.color_range, "matrix": info.matrix,
                         "transfer": info.transfer, "primaries": info.primaries, "start_time": info.start_time,
                         "frames": info.frames, "frame_count": info.frame_count},
                "describe": info.describe(),
                "filter_plain": frames.filter_chain(info, frames.Geometry.of(info), None),
                "filter_scaled": frames.filter_chain(info, target, crop),
                "seek_17": frames.seek_args(info, 17),
                "command": frames.ffmpeg_command("ffmpeg", info, 9, 4, target, crop, "pipe:1", with_times=True),
                "command_plain": frames.ffmpeg_command("ffmpeg", info, 0, 1, frames.Geometry.of(info), None, "x.yuv"),
                "frame_index_1_5": frames.frame_index(info, 1.5)})
    crops = []
    for sw, sh, ew, eh in ((3840, 2160, 3840, 1608), (1920, 1080, 1920, 800), (1440, 1080, 1920, 1080),
                           (3840, 2160, 3840, 2160), (720, 576, 1024, 576), (1920, 1080, 1440, 1080)):
        def fake(w, h):
            return probe_module.VideoInfo(Path("x"), 0, "h264", w, h, Fraction(24), "yuv420p", 8, 1, 1, 10.0, 0.0,
                                          "tv", "", "", "")
        c = frames.source_crop_for(fake(sw, sh), fake(ew, eh))
        crops.append({"source": [sw, sh], "encoded": [ew, eh],
                      "crop": None if c is None else [c.width, c.height, c.x, c.y]})
    return {"videos": out, "crops": crops}


def fraction_cases() -> list:
    cases = []
    for num, den in ((24000, 1001), (2997, 100), (30000, 1001), (119880, 1000), (3000003, 1000001),
                     (123456789, 987654), (60, 1), (1, 3), (2**40 + 1, 2**33)):
        f = Fraction(num, den)
        for limit in (1001000, 1000, 7):
            g = f.limit_denominator(limit)
            cases.append({"num": num, "den": den, "limit": limit, "result": [g.numerator, g.denominator]})
    return cases


def timeline_cases() -> dict:
    from afgs1wb.fullscan import Segment, TimelineModel, sample_sigmas
    from afgs1wb.tools import CancelToken
    params = [as_libaom_writes(p) for p in param_cases()]
    segments = [Segment(k * 37, (k + 1) * 37, p) for k, p in enumerate(params)]
    model = TimelineModel(segments, 10, 1, 1, Fraction(24000, 1001), len(params) * 37 + 5, seconds=123.456,
                          alignment=["ok", "unclear", "ok"], notes={"warnings": ["a warning"], "resumed": True})
    sample_sigmas(model, CancelToken())
    table = model.grain_table()
    return {"json": model.to_json(), "sigma_frames": model.sigma_frames, "sigma_values": model.sigma_values,
            "grain_table": [{"start": e.start, "end": e.end} for e in table],
            "segment_at": [{"index": i, "start": model.segment_at(i).start} for i in (-5, 0, 36, 37, 100, 10**6)]}


def read_grain_cases() -> list:
    sys.path.insert(0, str(ROOT / "tests"))
    import test_vvcgrain as t
    from afgs1wb.vvcgrain import read_grain
    streams = {
        "persistence": (t._sps(8) + t._fgc(1, persist=True) + t._slice(7, 0) + t._fgc(2) + t._slice(0, 4)
                        + t._slice(0, 2) + t._slice(0, 1) + t._slice(0, 3) + t._slice(0, 5)
                        + t._fgc(3, persist=True) + t._slice(0, 6) + t._CANCEL + t._slice(0, 7) + t._slice(0, 8)
                        + t._fgc(4, persist=True) + t._slice(0, 9) + t._slice(7, 0)),
        "order": (t._sps(4) + t._fgc(0) + t._slice(7, 0, 4) + t._fgc(1) + t._slice(0, 8, 4)
                  + t._fgc(2) + t._slice(0, 15, 4, non_reference=True) + t._fgc(3) + t._slice(0, 6, 4)
                  + t._fgc(4) + t._picture_header_nal(14, 4) + t._slice_after_picture_header(0)
                  + t._slice_after_picture_header(0) + t._fgc(5) + t._slice(0, 2, 4)
                  + t._fgc(6) + t._slice(3, 9, 4) + t._fgc(7) + t._slice(0, 13, 4)),
        "values": t._sps(8) + t._fgc(0, values=(40, 7, 5)) + t._slice(7, 0) + t._fgc(1, values=(-3, 9)) + t._slice(0, 1),
    }
    cases = []
    for name, data in streams.items():
        grain = read_grain(data)
        cases.append({"name": name, "stream": data.hex(),
                      "describe": [g.describe() if g else None for g in grain]})
    return cases


def write(name: str, data: dict) -> None:
    (OUT / name).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {name}")


if __name__ == "__main__":
    import os
    os.chdir(ROOT)
    write("params.json", {"cases": [params_json(p) for p in param_cases()]})
    write("synth.json", synth_cases())
    write("afgs1.json", afgs1_cases())
    write("grain_table.json", table_cases())
    write("tuning.json", tuning_cases())
    write("display.json", display_cases())
    write("vvc.json", vvc_cases())
    write("mkv.json", mkv_cases())
    write("engine.json", {"probe": probe_cases(), "fractions": fraction_cases(), "timeline": timeline_cases(),
                          "read_grain": read_grain_cases()})
