"""VVC (H.266) NAL unit handling for inserting AFGS1 prefix SEI messages.

Packets are expected in the length-prefixed layout used by Matroska and MP4
(the ``vvcC`` record gives the length-field size). Each packet is one access
unit; the AFGS1 message goes into its own prefix SEI NAL unit placed after any
AUD / parameter sets / existing prefix SEIs and before the picture header or
first slice, with the same nuh_layer_id and TemporalId as that picture, as
H.266 section 7.4.2.2 requires for SEI NAL units.
"""

from __future__ import annotations

import re

from .afgs1 import T35_PREFIX

NAL_PH = 19
NAL_PREFIX_SEI = 23
SEI_USER_DATA_REGISTERED_T35 = 4
_EMULATION = re.compile(b"\x00\x00(?=[\x00-\x03])")


def length_size_from_vvcc(extradata: bytes | None) -> int:
    """Return the NAL length-field size (1, 2 or 4) from a vvcC record."""
    if not extradata:
        raise ValueError("the VVC track has no vvcC codec private data")
    first = extradata[0]
    if first & 0xF8 != 0xF8:
        raise ValueError("codec private data is not a vvcC record")
    size = ((first >> 1) & 3) + 1
    if size == 3:
        raise ValueError("vvcC declares an invalid 3-byte NAL length")
    return size


def split_nals(data: bytes, length_size: int) -> list[bytes]:
    nals, pos = [], 0
    while pos < len(data):
        if pos + length_size > len(data):
            raise ValueError("truncated NAL length field")
        n = int.from_bytes(data[pos:pos + length_size], "big")
        pos += length_size
        if n < 2 or pos + n > len(data):
            raise ValueError("NAL length runs past the end of the packet")
        nals.append(data[pos:pos + n])
        pos += n
    return nals


def join_nals(nals: list[bytes], length_size: int) -> bytes:
    limit = 1 << (8 * length_size)
    out = bytearray()
    for nal in nals:
        if len(nal) >= limit:
            raise ValueError("NAL unit too large for the length field")
        out += len(nal).to_bytes(length_size, "big")
        out += nal
    return bytes(out)


def nal_type(nal: bytes) -> int:
    return nal[1] >> 3


def is_vcl(nal: bytes) -> bool:
    return nal_type(nal) <= 11


def escape_rbsp(rbsp: bytes) -> bytes:
    return _EMULATION.sub(b"\x00\x00\x03", rbsp)


def unescape_rbsp(data: bytes) -> bytes:
    return data.replace(b"\x00\x00\x03", b"\x00\x00")


def _sei_varint(value: int) -> bytes:
    return b"\xff" * (value // 255) + bytes((value % 255,))


def sei_nal(payload_type: int, payload: bytes, layer_id: int, tid_plus1: int) -> bytes:
    rbsp = _sei_varint(payload_type) + _sei_varint(len(payload)) + payload + b"\x80"
    header = bytes((layer_id & 0x3F, (NAL_PREFIX_SEI << 3) | tid_plus1))
    return header + escape_rbsp(rbsp)


def parse_sei_messages(nal: bytes) -> list[tuple[int, bytes]] | None:
    """Split an SEI NAL into (payload_type, payload) pairs, or None if malformed."""
    rbsp = unescape_rbsp(nal[2:])
    messages, pos = [], 0
    try:
        while pos < len(rbsp) and not (rbsp[pos] == 0x80 and not any(rbsp[pos + 1:])):
            ptype = 0
            while rbsp[pos] == 0xFF:
                ptype += 255
                pos += 1
            ptype += rbsp[pos]
            pos += 1
            size = 0
            while rbsp[pos] == 0xFF:
                size += 255
                pos += 1
            size += rbsp[pos]
            pos += 1
            if pos + size > len(rbsp):
                return None
            messages.append((ptype, rbsp[pos:pos + size]))
            pos += size
    except IndexError:
        return None
    return messages


def _is_afgs1(ptype: int, payload: bytes) -> bool:
    return ptype == SEI_USER_DATA_REGISTERED_T35 and payload.startswith(T35_PREFIX)


def _without_afgs1(nal: bytes) -> bytes | None:
    """Drop AFGS1 messages from a prefix SEI NAL; None if nothing is left."""
    messages = parse_sei_messages(nal)
    if messages is None or not any(_is_afgs1(t, p) for t, p in messages):
        return nal
    kept = [(t, p) for t, p in messages if not _is_afgs1(t, p)]
    if not kept:
        return None
    rbsp = b"".join(_sei_varint(t) + _sei_varint(len(p)) + p for t, p in kept) + b"\x80"
    return nal[:2] + escape_rbsp(rbsp)


def insert_t35(packet: bytes, length_size: int, t35_payload: bytes) -> tuple[bytes, bool]:
    """Return the packet with one AFGS1 prefix SEI added (replacing any old ones).

    The flag is False when the packet holds no picture (no VCL NAL unit), in
    which case it is returned unchanged.
    """
    nals = split_nals(packet, length_size)
    first_vcl = next((n for n in nals if is_vcl(n)), None)
    if first_vcl is None:
        return packet, False
    cleaned: list[bytes] = []
    for nal in nals:
        if nal_type(nal) == NAL_PREFIX_SEI:
            nal = _without_afgs1(nal)
            if nal is None:
                continue
        cleaned.append(nal)
    at = next(i for i, n in enumerate(cleaned) if is_vcl(n) or nal_type(n) == NAL_PH)
    sei = sei_nal(SEI_USER_DATA_REGISTERED_T35, t35_payload,
                  first_vcl[0] & 0x3F, first_vcl[1] & 0x07)
    cleaned.insert(at, sei)
    return join_nals(cleaned, length_size), True


def find_t35(packet: bytes, length_size: int) -> list[bytes]:
    """Return every AFGS1 T.35 payload found in the packet's prefix SEI NALs."""
    found = []
    for nal in split_nals(packet, length_size):
        if nal_type(nal) == NAL_PREFIX_SEI:
            for ptype, payload in parse_sei_messages(nal) or []:
                if _is_afgs1(ptype, payload):
                    found.append(payload)
    return found
