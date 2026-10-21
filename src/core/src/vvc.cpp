#include "fgw/vvc.h"

#include <algorithm>
#include <iterator>
#include <stdexcept>

#include "fgw/afgs1.h"

namespace fgw::vvc {

int length_size_from_vvcc(std::span<const std::uint8_t> extradata) {
    if (extradata.empty()) {
        throw std::invalid_argument("the VVC track has no vvcC codec private data");
    }
    const std::uint8_t first = extradata[0];
    if ((first & 0xF8) != 0xF8) {
        throw std::invalid_argument("codec private data is not a vvcC record");
    }
    const int size = ((first >> 1) & 3) + 1;
    if (size == 3) {
        throw std::invalid_argument("vvcC declares an invalid 3-byte NAL length");
    }
    return size;
}

std::vector<std::span<const std::uint8_t>> split_nals(std::span<const std::uint8_t> packet, int length_size) {
    std::vector<std::span<const std::uint8_t>> nals;
    std::size_t pos = 0;
    const auto length_bytes = static_cast<std::size_t>(length_size);
    while (pos < packet.size()) {
        if (pos + length_bytes > packet.size()) {
            throw std::invalid_argument("truncated NAL length field");
        }
        std::size_t length = 0;
        for (std::size_t i = 0; i < length_bytes; ++i) {
            length = (length << 8) | packet[pos + i];
        }
        pos += length_bytes;
        if (length < 2 || pos + length > packet.size()) {
            throw std::invalid_argument("NAL length runs past the end of the packet");
        }
        nals.push_back(packet.subspan(pos, length));
        pos += length;
    }
    return nals;
}

namespace {

void append_nal(Bytes& out, std::span<const std::uint8_t> nal, int length_size) {
    if (length_size < 4 && nal.size() >= (std::size_t{1} << (8 * length_size))) {
        throw std::invalid_argument("NAL unit too large for the length field");
    }
    for (int shift = 8 * (length_size - 1); shift >= 0; shift -= 8) {
        out.push_back(static_cast<std::uint8_t>(nal.size() >> shift));
    }
    out.insert(out.end(), nal.begin(), nal.end());
}

// An SEI payload type or size: 0xFF for each full 255, then the rest.
void append_sei_number(Bytes& out, std::size_t value) {
    out.insert(out.end(), value / 255, 0xFF);
    out.push_back(static_cast<std::uint8_t>(value % 255));
}

bool is_afgs1(const SeiMessage& message) {
    return message.payload_type == sei_user_data_registered_t35 && message.payload.size() >= t35_prefix.size() &&
           std::equal(t35_prefix.begin(), t35_prefix.end(), message.payload.begin());
}

Bytes sei_rbsp(const std::vector<SeiMessage>& messages) {
    Bytes rbsp;
    for (const SeiMessage& message : messages) {
        append_sei_number(rbsp, static_cast<std::size_t>(message.payload_type));
        append_sei_number(rbsp, message.payload.size());
        rbsp.insert(rbsp.end(), message.payload.begin(), message.payload.end());
    }
    rbsp.push_back(0x80);  // rbsp_trailing_bits
    return rbsp;
}

}  // namespace

Bytes escape_rbsp(std::span<const std::uint8_t> rbsp) {
    Bytes out;
    out.reserve(rbsp.size() + rbsp.size() / 64);
    int zeros = 0;
    for (const std::uint8_t byte : rbsp) {
        if (zeros >= 2 && byte <= 3) {
            out.push_back(3);
            zeros = 0;
        }
        out.push_back(byte);
        zeros = byte == 0 ? zeros + 1 : 0;
    }
    return out;
}

Bytes unescape_rbsp(std::span<const std::uint8_t> data) {
    Bytes out;
    out.reserve(data.size());
    std::size_t i = 0;
    while (i < data.size()) {
        if (i + 2 < data.size() && data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 3) {
            out.push_back(0);
            out.push_back(0);
            i += 3;
        } else {
            out.push_back(data[i++]);
        }
    }
    return out;
}

Bytes sei_nal(int payload_type, std::span<const std::uint8_t> payload, int layer_id, int temporal_id_plus1) {
    const Bytes rbsp = sei_rbsp({{payload_type, Bytes(payload.begin(), payload.end())}});
    Bytes nal = {static_cast<std::uint8_t>(layer_id & 0x3F),
                 static_cast<std::uint8_t>((nal_prefix_sei << 3) | temporal_id_plus1)};
    const Bytes escaped = escape_rbsp(rbsp);
    nal.insert(nal.end(), escaped.begin(), escaped.end());
    return nal;
}

std::optional<std::vector<SeiMessage>> parse_sei_messages(std::span<const std::uint8_t> nal) {
    const Bytes rbsp = unescape_rbsp(nal.subspan(2));
    std::vector<SeiMessage> messages;
    std::size_t pos = 0;
    // Each loop reads one message; the rest is the trailing bits when it is
    // 0x80 followed by zeros alone.
    const auto at_trailing_bits = [&rbsp](std::size_t at) {
        return rbsp[at] == 0x80 && std::all_of(rbsp.begin() + static_cast<std::ptrdiff_t>(at) + 1, rbsp.end(),
                                               [](std::uint8_t b) { return b == 0; });
    };
    const auto read_number = [&rbsp, &pos]() -> std::optional<std::size_t> {
        std::size_t value = 0;
        while (pos < rbsp.size() && rbsp[pos] == 0xFF) {
            value += 255;
            ++pos;
        }
        if (pos >= rbsp.size()) {
            return std::nullopt;
        }
        return value + rbsp[pos++];
    };
    while (pos < rbsp.size() && !at_trailing_bits(pos)) {
        const std::optional<std::size_t> type = read_number();
        const std::optional<std::size_t> size = type ? read_number() : std::nullopt;
        if (!size || pos + *size > rbsp.size()) {
            return std::nullopt;
        }
        messages.push_back({static_cast<int>(*type), Bytes(rbsp.begin() + static_cast<std::ptrdiff_t>(pos),
                                                           rbsp.begin() + static_cast<std::ptrdiff_t>(pos + *size))});
        pos += *size;
    }
    return messages;
}

namespace {

// A prefix SEI NAL unit without its AFGS1 messages: nothing if none is left,
// the NAL unit as it was if it had none or does not parse.
std::optional<Bytes> without_afgs1(std::span<const std::uint8_t> nal) {
    const std::optional<std::vector<SeiMessage>> messages = parse_sei_messages(nal);
    if (!messages || std::none_of(messages->begin(), messages->end(), is_afgs1)) {
        return Bytes(nal.begin(), nal.end());
    }
    std::vector<SeiMessage> kept;
    std::copy_if(messages->begin(), messages->end(), std::back_inserter(kept),
                 [](const SeiMessage& m) { return !is_afgs1(m); });
    if (kept.empty()) {
        return std::nullopt;
    }
    Bytes out(nal.begin(), nal.begin() + 2);
    const Bytes escaped = escape_rbsp(sei_rbsp(kept));
    out.insert(out.end(), escaped.begin(), escaped.end());
    return out;
}

}  // namespace

Inserted insert_t35(std::span<const std::uint8_t> packet, int length_size, std::span<const std::uint8_t> t35_payload) {
    const auto nals = split_nals(packet, length_size);
    const auto first_vcl = std::find_if(nals.begin(), nals.end(), [](auto nal) { return is_vcl(nal); });
    if (first_vcl == nals.end()) {
        return {Bytes(packet.begin(), packet.end()), false};
    }
    const Bytes sei = sei_nal(sei_user_data_registered_t35, t35_payload, (*first_vcl)[0] & 0x3F, (*first_vcl)[1] & 0x07);
    Inserted result{{}, true};
    result.packet.reserve(packet.size() + sei.size() + static_cast<std::size_t>(length_size));
    bool sei_written = false;
    for (const auto nal : nals) {
        if (!sei_written && (is_vcl(nal) || nal_type(nal) == nal_picture_header)) {
            append_nal(result.packet, sei, length_size);
            sei_written = true;
        }
        if (nal_type(nal) == nal_prefix_sei) {
            if (const std::optional<Bytes> kept = without_afgs1(nal)) {
                append_nal(result.packet, *kept, length_size);
            }
            continue;
        }
        append_nal(result.packet, nal, length_size);
    }
    return result;
}

std::set<int> prefix_sei_types(std::span<const std::uint8_t> packet, int length_size) {
    std::set<int> types;
    for (const auto nal : split_nals(packet, length_size)) {
        if (nal_type(nal) == nal_prefix_sei) {
            for (const SeiMessage& message : parse_sei_messages(nal).value_or(std::vector<SeiMessage>{})) {
                types.insert(message.payload_type);
            }
        }
    }
    return types;
}

std::vector<Bytes> find_t35(std::span<const std::uint8_t> packet, int length_size) {
    std::vector<Bytes> found;
    for (const auto nal : split_nals(packet, length_size)) {
        if (nal_type(nal) == nal_prefix_sei) {
            for (SeiMessage& message : parse_sei_messages(nal).value_or(std::vector<SeiMessage>{})) {
                if (is_afgs1(message)) {
                    found.push_back(std::move(message.payload));
                }
            }
        }
    }
    return found;
}

}  // namespace fgw::vvc
