// H.266 (VVC) NAL units: inserting AFGS1 prefix SEI messages into the
// length-prefixed packets of Matroska and MP4, and the SEI messages of Annex B
// streams.
//
// Each packet is one access unit. The AFGS1 message goes into a prefix SEI NAL
// unit of its own, after any AUD, parameter sets and existing prefix SEIs and
// before the picture header or first slice, with that picture's nuh_layer_id
// and TemporalId, as H.266 section 7.4.2.2 requires for SEI NAL units.
#pragma once

#include <cstdint>
#include <optional>
#include <set>
#include <span>
#include <vector>

namespace fgw::vvc {

using Bytes = std::vector<std::uint8_t>;

inline constexpr int nal_picture_header = 19;
inline constexpr int nal_prefix_sei = 23;
inline constexpr int sei_user_data_registered_t35 = 4;
inline constexpr int sei_mastering_display = 137;
inline constexpr int sei_content_light_level = 144;

// The NAL length-field size (1, 2 or 4) a vvcC record declares. Throws
// std::invalid_argument for something that is not one.
int length_size_from_vvcc(std::span<const std::uint8_t> extradata);

// The NAL units of a length-prefixed packet. Throws std::invalid_argument
// for a malformed one.
std::vector<std::span<const std::uint8_t>> split_nals(std::span<const std::uint8_t> packet, int length_size);

inline int nal_type(std::span<const std::uint8_t> nal) { return nal[1] >> 3; }
inline bool is_vcl(std::span<const std::uint8_t> nal) { return nal_type(nal) <= 11; }

// Emulation prevention: 0x000003 inserted before 0x0000 0x/1x/2x/3x, and
// taken out again.
Bytes escape_rbsp(std::span<const std::uint8_t> rbsp);
Bytes unescape_rbsp(std::span<const std::uint8_t> data);

// A prefix SEI NAL unit holding one message.
Bytes sei_nal(int payload_type, std::span<const std::uint8_t> payload, int layer_id, int temporal_id_plus1);

struct SeiMessage {
    int payload_type = 0;
    Bytes payload;
};

// The messages of an SEI NAL unit (its two header bytes first), or nothing
// if it is malformed.
std::optional<std::vector<SeiMessage>> parse_sei_messages(std::span<const std::uint8_t> nal);

struct Inserted {
    Bytes packet;
    bool is_picture = false;  // false: no VCL NAL unit, the packet is returned as it was
};

// The packet with one AFGS1 prefix SEI holding `t35_payload` (replacing any
// AFGS1 messages it had).
Inserted insert_t35(std::span<const std::uint8_t> packet, int length_size, std::span<const std::uint8_t> t35_payload);

// The payload types of the packet's prefix SEI messages.
std::set<int> prefix_sei_types(std::span<const std::uint8_t> packet, int length_size);

// Every AFGS1 T.35 payload in the packet's prefix SEI NAL units.
std::vector<Bytes> find_t35(std::span<const std::uint8_t> packet, int length_size);

}  // namespace fgw::vvc
