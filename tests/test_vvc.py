from afgs1wb.afgs1 import PictureFormat, encode_t35
from afgs1wb.vvc import (NAL_PH, NAL_PREFIX_SEI, escape_rbsp, find_t35, insert_t35, join_nals,
                         length_size_from_vvcc, nal_type, parse_sei_messages, sei_nal,
                         split_nals, unescape_rbsp)

from conftest import sample_params

T35 = encode_t35(sample_params(), PictureFormat(1920, 1080, 10), 77)


def nal(kind: int, body: bytes, tid_plus1: int = 1, layer: int = 0) -> bytes:
    return bytes((layer, (kind << 3) | tid_plus1)) + body


def test_length_size_from_vvcc():
    assert length_size_from_vvcc(b"\xff\x00") == 4
    assert length_size_from_vvcc(b"\xfb\x00") == 2
    assert length_size_from_vvcc(b"\xf9\x00") == 1


def test_escape_roundtrip():
    raw = bytes([0, 0, 0, 0, 1, 0, 0, 2, 0, 0, 3, 7, 0, 0])
    escaped = escape_rbsp(raw)
    assert b"\x00\x00\x00" not in escaped and b"\x00\x00\x01" not in escaped
    assert unescape_rbsp(escaped) == raw


def test_insert_before_picture_header_with_picture_temporal_id():
    packet = join_nals([nal(16, b"pps"), nal(NAL_PH, b"ph", 4), nal(1, b"slice", 4)], 4)
    out, inserted = insert_t35(packet, 4, T35)
    assert inserted
    nals = split_nals(out, 4)
    assert [nal_type(n) for n in nals] == [16, NAL_PREFIX_SEI, NAL_PH, 1]
    sei = nals[1]
    assert sei[1] & 7 == 4                      # TemporalId copied from the picture
    assert parse_sei_messages(sei) == [(4, T35)]


def test_reinsert_replaces_old_metadata_and_keeps_other_sei():
    other = sei_nal(137, b"\x01\x02mastering", 0, 1)
    old = sei_nal(4, encode_t35(sample_params(), PictureFormat(1920, 1080, 10), 1), 0, 1)
    packet = join_nals([other, old, nal(8, b"idr")], 4)
    out, _ = insert_t35(packet, 4, T35)
    assert find_t35(out, 4) == [T35]
    kept = [parse_sei_messages(n) for n in split_nals(out, 4) if nal_type(n) == NAL_PREFIX_SEI]
    assert [(137, b"\x01\x02mastering")] in kept


def test_non_picture_packet_untouched():
    packet = join_nals([nal(20, b"\x10")], 4)
    assert insert_t35(packet, 4, T35) == (packet, False)
