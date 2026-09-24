"""Insert AFGS1 registered T.35 prefix SEI messages into VVC packets."""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import re
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .grain import GrainEntry, make_afgs1_t35, make_prefix_sei_nal, parameter_at


def _annexb_units(data: bytes) -> list[tuple[bytes, bytes]] | None:
    matches = list(re.finditer(b"\\x00\\x00(?:\\x00\\x01|\\x01)", data))
    if not matches or matches[0].start() != 0:
        return None
    units: list[tuple[bytes, bytes]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(data)
        startcode = match.group(0)
        nal = data[match.end():end]
        while nal.endswith(b"\x00"):
            nal = nal[:-1]
        if nal:
            units.append((startcode, nal))
    return units


def _length_prefixed_units(data: bytes, length_size: int) -> list[tuple[bytes, bytes]] | None:
    if length_size not in (1, 2, 3, 4):
        return None
    offset = 0
    units: list[tuple[bytes, bytes]] = []
    while offset < len(data):
        if offset + length_size > len(data):
            return None
        prefix = data[offset:offset + length_size]
        size = int.from_bytes(prefix, "big")
        offset += length_size
        if size < 2 or offset + size > len(data):
            return None
        units.append((prefix, data[offset:offset + size]))
        offset += size
    return units if units else None


def _parse_units(data: bytes, extradata: bytes | None) -> tuple[list[tuple[bytes, bytes]], bool, int]:
    annexb = _annexb_units(data)
    if annexb is not None:
        return annexb, True, 0
    preferred = None
    if extradata and len(extradata) >= 2:
        preferred = (extradata[1] & 0x03) + 1
    sizes = [preferred] if preferred else []
    sizes.extend(size for size in (4, 2, 1, 3) if size != preferred)
    for size in sizes:
        units = _length_prefixed_units(data, size)
        if units is not None:
            return units, False, size
    raise RuntimeError("Could not parse the encoded VVC packet as Annex B or length-prefixed NAL units")


def _pack_units(units: list[tuple[bytes, bytes]], annexb: bool, length_size: int) -> bytes:
    out = bytearray()
    for prefix, nal in units:
        if annexb:
            out.extend(prefix)
            out.extend(nal)
        else:
            if len(nal) >= (1 << (8 * length_size)):
                raise RuntimeError("Injected SEI is too large for this NAL length field")
            out.extend(len(nal).to_bytes(length_size, "big"))
            out.extend(nal)
    return bytes(out)


def _to_annexb(data: bytes, extradata: bytes | None) -> bytes:
    """Convert one VVC packet to Annex B for PyAV's mux-only VVC stream."""
    units, _, _ = _parse_units(data, extradata)
    return b"".join(b"\x00\x00\x00\x01" + nal for _, nal in units)


def _replace_packet_data(packet, data: bytes):
    """Return a new packet with the original timestamps, flags, and side data."""
    import av

    replacement = av.Packet(data)
    for attribute in ("pts", "dts", "duration", "time_base", "is_keyframe", "is_corrupt"):
        try:
            setattr(replacement, attribute, getattr(packet, attribute))
        except (AttributeError, ValueError):
            pass
    for side_data in packet.iter_sidedata():
        replacement.set_sidedata(side_data)
    return replacement


def _vvc_timing_plan(source: Path, stream_index: int, time_base: Fraction,
                     frame_rate: Fraction, start_time: int | None
                     ) -> tuple[int, bool, int, int]:
    """Find a presentation origin and a monotonic DTS schedule for VVC packets."""
    import av

    default_duration = max(1, round((Fraction(1, 1) / frame_rate) / time_base))
    packet_count = 0
    elapsed = 0
    dts_origin_candidate: int | None = None
    presentation_origin: int | None = None
    last_dts: int | None = None
    needs_synthesized_dts = False

    with av.open(str(source), mode="r") as timing_container:
        timing_stream = next((s for s in timing_container.streams
                              if s.index == stream_index), None)
        if timing_stream is None:
            raise RuntimeError("Could not reopen the VVC stream to inspect its packet timing")
        for packet in timing_container.demux(timing_stream):
            if packet.size <= 0:
                continue
            packet_time_base = packet.time_base or time_base
            to_stream_ticks = lambda value: round(Fraction(value) * packet_time_base / time_base)
            pts = to_stream_ticks(packet.pts) if packet.pts is not None else None
            dts = to_stream_ticks(packet.dts) if packet.dts is not None else None
            duration = to_stream_ticks(packet.duration) if packet.duration else default_duration
            if duration <= 0:
                duration = default_duration

            if dts is None or (last_dts is not None and dts <= last_dts):
                needs_synthesized_dts = True
            if dts is not None:
                last_dts = dts
            if pts is not None:
                presentation_origin = pts if presentation_origin is None else min(presentation_origin, pts)
                candidate = pts - elapsed
                dts_origin_candidate = (candidate if dts_origin_candidate is None
                                        else min(dts_origin_candidate, candidate))
            elapsed += duration
            packet_count += 1

    if packet_count == 0:
        raise RuntimeError("The VVC stream contains no packets")
    if presentation_origin is None:
        presentation_origin = start_time or 0
    dts_origin = (dts_origin_candidate if dts_origin_candidate is not None else
                  (start_time or 0))
    return presentation_origin, needs_synthesized_dts, dts_origin, default_duration


def _unescape_rbsp(data: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 3:
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _escape_rbsp(data: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _write_sei_value(value: int) -> bytes:
    out = bytearray()
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)
    return bytes(out)


def _remove_existing_afgs1(nal: bytes) -> bytes | None:
    """Remove AFGS1 T.35 messages while retaining other prefix-SEI messages."""
    if len(nal) < 3 or ((nal[1] >> 3) & 0x1F) != 23:
        return nal
    rbsp = _unescape_rbsp(nal[2:])
    pos = 0
    messages: list[tuple[int, bytes]] = []
    try:
        while pos < len(rbsp):
            if rbsp[pos] == 0x80 and not any(rbsp[pos + 1:]):
                break
            payload_type = 0
            while rbsp[pos] == 255:
                payload_type += 255
                pos += 1
            payload_type += rbsp[pos]
            pos += 1
            payload_size = 0
            while rbsp[pos] == 255:
                payload_size += 255
                pos += 1
            payload_size += rbsp[pos]
            pos += 1
            payload = rbsp[pos:pos + payload_size]
            if len(payload) != payload_size:
                return nal
            pos += payload_size
            if not (payload_type == 4 and payload.startswith(b"\xB5\x58\x90\x01")):
                messages.append((payload_type, payload))
    except IndexError:
        return nal
    if len(messages) == 0:
        return None
    rebuilt = bytearray()
    for payload_type, payload in messages:
        rebuilt.extend(_write_sei_value(payload_type))
        rebuilt.extend(_write_sei_value(len(payload)))
        rebuilt.extend(payload)
    rebuilt.append(0x80)
    return nal[:2] + _escape_rbsp(bytes(rebuilt))


def add_prefix_sei(packet_data: bytes, t35: bytes, extradata: bytes | None) -> tuple[bytes, bool, bool]:
    units, annexb, length_size = _parse_units(packet_data, extradata)
    cleaned: list[tuple[bytes, bytes]] = []
    changed = False
    for prefix, nal in units:
        replacement = _remove_existing_afgs1(nal)
        if replacement != nal:
            changed = True
        if replacement is not None:
            cleaned.append((prefix, replacement))
    units = cleaned
    first_vcl = next((i for i, (_, nal) in enumerate(units)
                      if len(nal) >= 2 and ((nal[1] >> 3) & 0x1F) <= 11), None)
    if first_vcl is None:
        return (_pack_units(units, annexb, length_size) if changed else packet_data), False, changed
    nal = make_prefix_sei_nal(t35)
    prefix = b"\x00\x00\x00\x01" if annexb else b""
    units.insert(first_vcl, (prefix, nal))
    return _pack_units(units, annexb, length_size), True, True


def inject_afgs1(
    input_path: str | Path,
    output_path: str | Path,
    entries: list[GrainEntry],
    width: int,
    height: int,
    bit_depth: int,
    subsampling_x: int,
    subsampling_y: int,
    strength: float,
    seed_offset: int,
    grain_scale_delta: int,
    overlap: bool,
    restricted_range: bool,
    analysis_prefix_seconds: float = 0.0,
    progress: Callable[[int, str], None] | None = None,
) -> int:
    """Copy streams into MKV while adding one AFGS1 message to each VVC AU."""
    try:
        import av
    except ImportError as exc:
        raise RuntimeError("PyAV is not installed. Install the listed requirements first.") from exc

    source = Path(input_path).expanduser().resolve()
    target = Path(output_path).expanduser().resolve()
    if source == target:
        raise RuntimeError("Output path must differ from the encoded input")
    if target.suffix.lower() != ".mkv":
        raise RuntimeError("AFGS1 VVC output is currently written as Matroska (.mkv)")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + f".afgs1-{uuid4().hex[:8]}.mkv")

    input_container = None
    output_container = None
    inserted = 0
    picture_index = 0
    video_input = None
    try:
        input_container = av.open(str(source), mode="r")
        video_input = next((s for s in input_container.streams if s.type == "video"), None)
        if video_input is None:
            raise RuntimeError("The encoded file has no video stream")
        codec = (video_input.codec_context.name or "").lower()
        if "vvc" not in codec and "h266" not in codec:
            raise RuntimeError(f"Encoded video is {codec!r}, not VVC/H.266")

        stream_time_base = video_input.time_base or Fraction(1, 25)
        frame_rate = video_input.average_rate or video_input.base_rate or Fraction(25, 1)
        timestamp_origin, synthesize_dts, dts_origin, default_duration = _vvc_timing_plan(
            source, video_input.index, stream_time_base, frame_rate, video_input.start_time)

        output_container = av.open(str(temporary), mode="w", format="matroska")
        stream_map = {}
        for stream in input_container.streams:
            try:
                if stream.index == video_input.index:
                    if not hasattr(output_container, "add_mux_stream"):
                        raise RuntimeError(
                            "This PyAV version cannot mux pre-encoded VVC packets. "
                            "Upgrade to PyAV 18.0 or newer."
                        )
                    stream_map[stream.index] = output_container.add_mux_stream(
                        codec,
                        rate=frame_rate,
                        width=video_input.codec_context.width,
                        height=video_input.codec_context.height,
                        time_base=video_input.time_base or Fraction(1, 25),
                    )
                    stream_map[stream.index].metadata.update(stream.metadata)
                    stream_map[stream.index].disposition = stream.disposition
                else:
                    stream_map[stream.index] = output_container.add_stream_from_template(stream)
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc).startswith("This PyAV version"):
                    raise
                raise RuntimeError(f"Cannot copy {stream.type} stream {stream.index} into Matroska: {exc}") from exc
        output_container.metadata.update(input_container.metadata)
        extradata = video_input.codec_context.extradata
        total = max(1, int(video_input.frames or 0))
        dts_elapsed = 0
        for packet in input_container.demux():
            input_stream_index = packet.stream.index
            if packet.size <= 0 or input_stream_index not in stream_map:
                continue
            if input_stream_index == video_input.index:
                packet_time_base = packet.time_base or stream_time_base
                if packet.time_base is None:
                    packet.time_base = stream_time_base
                elif packet_time_base != stream_time_base:
                    packet.rescale_ts(stream_time_base)
                packet_duration = packet.duration or default_duration
                if packet_duration <= 0:
                    packet_duration = default_duration
                if synthesize_dts:
                    packet.dts = dts_origin + dts_elapsed
                dts_elapsed += packet_duration
                pts = packet.pts
                seconds = 0.0
                if pts is not None:
                    seconds = float((pts - timestamp_origin) * stream_time_base)
                elif packet.dts is not None:
                    seconds = float((packet.dts - timestamp_origin) * stream_time_base)
                model_seconds = max(0.0, seconds - analysis_prefix_seconds)
                p = deepcopy(parameter_at(entries, max(0, round(model_seconds * 10_000_000))))
                # Advance the LFSR seed for every output picture so the grain
                # pattern evolves over time instead of repeating as a still.
                p.random_seed = (p.random_seed + picture_index) & 0xFFFF
                t35 = make_afgs1_t35(
                    p, width, height, bit_depth, subsampling_x, subsampling_y,
                    strength, seed_offset, grain_scale_delta, overlap, restricted_range)
                modified, did_insert, changed = add_prefix_sei(bytes(packet), t35, extradata)
                # VVC has no encoder in PyAV's Windows wheels, so it must use
                # add_mux_stream() and Annex B packets. PyAV's Matroska muxer
                # derives the VVC codec private data from the initial Annex B
                # parameter sets before it writes the file header.
                annexb_packet = _to_annexb(modified, extradata)
                if changed or annexb_packet != bytes(packet):
                    # Packet.update() can only overwrite buffers of the same
                    # size. SEI insertion changes packet lengths, so create a
                    # fresh packet and keep its timing and side data.
                    packet = _replace_packet_data(packet, annexb_packet)
                if did_insert:
                    inserted += 1
                    picture_index += 1
                    if progress and inserted % 200 == 0:
                        progress(min(99, inserted * 100 // total), f"Added grain metadata to {inserted} pictures…")
            if packet.size <= 0:
                continue
            packet.stream = stream_map[input_stream_index]
            output_container.mux(packet)
        output_container.close()
        output_container = None
        input_container.close()
        input_container = None
        if inserted == 0:
            raise RuntimeError("No VVC picture packets were found for AFGS1 insertion")
        temporary.replace(target)
        return inserted
    except Exception:
        if output_container is not None:
            output_container.close()
        if input_container is not None:
            input_container.close()
        if temporary.exists():
            temporary.unlink()
        raise
