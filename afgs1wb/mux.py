"""Write an MKV whose VVC pictures each carry an AFGS1 prefix SEI.

1. Demux the encode's VVC packets with PyAV (reading only - PyAV cannot write
   VVC without an encoder).
2. Insert one AFGS1 T.35 SEI per picture (per-picture seed, see
   :func:`afgs1.picture_seed`) and store the packets with their original
   presentation times in a video-only MKV (:mod:`mkvwriter`).
3. Let mkvmerge merge that track with every other track, chapter, attachment
   and tag of the original file, copying the video track's colour/HDR
   properties onto the new track.
4. Verify the result has the same number of video packets and the same
   leading timestamps as the input.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import json
import os
import tempfile

from .afgs1 import PictureFormat, T35Template, picture_seed
from .grain_params import GrainParams
from .mkvwriter import MatroskaVideoWriter
from .probe import VideoInfo
from .tools import CancelToken, ToolError, Tools, run, run_text
from .vvc import insert_t35, length_size_from_vvcc


@dataclass
class WriteReport:
    output: Path
    pictures: int
    packets: int


def picture_format(info: VideoInfo) -> PictureFormat:
    return PictureFormat(info.width, info.height, info.bit_depth,
                         info.subsampling_x, info.subsampling_y)


def _packet_pts(path: Path, stream: int, ffprobe: str, count: int, cancel) -> list[str]:
    out = run_text([ffprobe, "-v", "error", "-select_streams", str(stream), "-read_intervals",
                    f"%+#{count}", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                    str(path)], cancel)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _count_packets(path: Path, stream: int, ffprobe: str, cancel) -> int:
    out = run_text([ffprobe, "-v", "error", "-select_streams", str(stream), "-count_packets",
                    "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)], cancel)
    return int(out.strip().split(",")[0])


# mkvmerge -J video-track property -> option that sets it on the new track.
_TRACK_OPTIONS = {
    "language": "--language", "track_name": "--track-name",
    "default_track": "--default-track-flag", "forced_track": "--forced-display-flag",
    "display_dimensions": "--display-dimensions",
    "color_matrix_coefficients": "--color-matrix-coefficients",
    "color_bits_per_channel": "--color-bits-per-channel",
    "chroma_subsample": "--chroma-subsample", "chroma_siting": "--chroma-siting",
    "color_range": "--color-range",
    "color_transfer_characteristics": "--color-transfer-characteristics",
    "color_primaries": "--color-primaries",
    "max_content_light": "--max-content-light", "max_frame_light": "--max-frame-light",
    "chromaticity_coordinates": "--chromaticity-coordinates",
    "white_color_coordinates": "--white-color-coordinates",
    "max_luminance": "--max-luminance", "min_luminance": "--min-luminance",
}


def _merge_with_mkvmerge(mkvmerge: str, video: Path, original: Path, output: Path,
                         cancel: CancelToken) -> None:
    """Our VVC track first, then every other track, chapter, tag and attachment.

    ffmpeg cannot be used here: its Matroska demuxer does not rebuild decode
    timestamps for VVC B-pictures, so ``ffmpeg -c copy`` rewrites them and
    breaks the presentation order (it does so even for an untouched VVC MKV).
    mkvmerge works on presentation timestamps only, which is what MKV stores.
    """
    info = json.loads(run_text([mkvmerge, "-J", str(original)], cancel))
    videos = [t for t in info.get("tracks", []) if t.get("type") == "video"]
    main = next((t for t in videos if "VVC" in (t.get("codec", "") + t.get("properties", {})
                                                .get("codec_id", ""))), videos[0] if videos else None)
    if main is None:
        raise RuntimeError("mkvmerge found no video track in the encode")
    options: list[str] = []
    for key, flag in _TRACK_OPTIONS.items():
        value = main.get("properties", {}).get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = int(value)
        options += [flag, f"0:{value}"]
    title = info.get("container", {}).get("properties", {}).get("title")
    if title:
        options += ["--title", title]
    other_video = [str(t["id"]) for t in videos if t is not main]
    keep_video = ["-d", ",".join(other_video)] if other_video else ["-D"]
    command = [mkvmerge, "--quiet", "-o", str(output), *options, str(video),
               *keep_video, str(original)]
    try:
        run(command, cancel)
    except ToolError as exc:
        # mkvmerge exits with 1 for warnings; the file is complete in that case.
        if "exit code 1)" not in str(exc) or not output.is_file():
            raise


def write_afgs1_mkv(tools: Tools, encoded: VideoInfo, output: str | Path, params: GrainParams,
                    base_seed: int, progress, cancel: CancelToken) -> WriteReport:
    import av

    output = Path(output)
    if output.suffix.lower() != ".mkv":
        raise ValueError("the output must be a .mkv file")
    if output.resolve() == encoded.path.resolve():
        raise ValueError("the output must not overwrite the encoded input")
    template = T35Template(params, picture_format(encoded))
    fps = float(encoded.fps)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="afgs1wb-mux-", dir=output.parent) as tmp:
        video_path = Path(tmp) / "video.mkv"
        pictures = packets = 0
        with av.open(str(encoded.path)) as container:
            stream = container.streams[encoded.stream_index]
            length_size = length_size_from_vvcc(stream.codec_context.extradata)
            time_base: Fraction = stream.time_base
            writer = MatroskaVideoWriter(video_path, stream.codec_context.extradata,
                                         encoded.width, encoded.height,
                                         int(round(1e9 / fps)))
            try:
                total = encoded.frame_count
                for packet in container.demux(stream):
                    if packet.size == 0:
                        continue
                    if packet.pts is None:
                        raise RuntimeError(f"packet {packets} of the VVC stream has no timestamp")
                    seconds = float(packet.pts * time_base)
                    index = int(round((seconds - encoded.first_pts) * fps))
                    data, is_picture = insert_t35(bytes(packet), length_size,
                                                  template.for_seed(picture_seed(base_seed, index)))
                    pictures += is_picture
                    duration = int(round(float(packet.duration * time_base) * 1000)) if packet.duration else 0
                    writer.write(data, int(round(seconds * 1000)), packet.is_keyframe, duration)
                    packets += 1
                    if packets % 500 == 0:
                        cancel.check()
                        progress(min(0.9, 0.9 * packets / total),
                                 f"Adding grain metadata: {packets:,} of ~{total:,} pictures")
            except BaseException:
                writer.abort()
                raise
            writer.close()

        progress(0.9, "Merging audio, subtitles and chapters from the encode…")
        merged = Path(tmp) / "merged.mkv"
        _merge_with_mkvmerge(tools.mkvmerge, video_path, encoded.path, merged, cancel)

        progress(0.95, "Checking the written file…")
        written = _count_packets(merged, 0, tools.ffprobe, cancel)
        if written != packets:
            raise RuntimeError(f"verification failed: wrote {packets} VVC packets but the "
                               f"output contains {written}")
        before = _packet_pts(encoded.path, encoded.stream_index, tools.ffprobe, 64, cancel)
        after = _packet_pts(merged, 0, tools.ffprobe, 64, cancel)
        if [round(float(t), 3) for t in before] != [round(float(t), 3) for t in after]:
            raise RuntimeError("verification failed: the output's video timestamps differ from the "
                               f"input (first ones: {before[:6]} vs {after[:6]})")
        os.replace(merged, output)
    progress(1.0, "Done")
    return WriteReport(output, pictures, packets)
