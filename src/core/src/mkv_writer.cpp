#include "fgw/mkv_writer.h"

#include <algorithm>
#include <bit>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <string_view>

namespace fgw {

namespace {

using Bytes = std::vector<std::uint8_t>;

constexpr std::string_view codec_id_vvc = "V_MPEGI/ISO/VVC";
constexpr std::string_view app_name = "afgs1wb";       // MuxingApp and WritingApp, as the Python app wrote
constexpr std::int64_t timestamp_scale_ns = 1'000'000;  // block timestamps in milliseconds
constexpr std::int64_t cluster_span_ms = 30'000;        // block times are 16-bit offsets from their cluster's

void append_big_endian(Bytes& out, std::uint64_t value, int bytes) {
    for (int shift = 8 * (bytes - 1); shift >= 0; shift -= 8) {
        out.push_back(static_cast<std::uint8_t>(value >> shift));
    }
}

int byte_length(std::uint64_t value) { return std::max(1, static_cast<int>((std::bit_width(value) + 7) / 8)); }

Bytes id(std::uint32_t value) {
    Bytes out;
    append_big_endian(out, value, byte_length(value));
    return out;
}

// An EBML element size as the shortest variable-length integer.
Bytes size_field(std::uint64_t n) {
    for (int length = 1; length <= 8; ++length) {
        const std::uint64_t limit = (std::uint64_t{1} << (7 * length)) - 1;
        if (n < limit) {
            Bytes out;
            append_big_endian(out, (std::uint64_t{1} << (7 * length)) | n, length);
            return out;
        }
    }
    throw std::runtime_error("Matroska element too large");
}

Bytes element(std::uint32_t element_id, const Bytes& payload) {
    Bytes out = id(element_id);
    const Bytes size = size_field(payload.size());
    out.insert(out.end(), size.begin(), size.end());
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
}

Bytes uint_element(std::uint32_t element_id, std::uint64_t value) {
    Bytes payload;
    append_big_endian(payload, value, byte_length(value));
    return element(element_id, payload);
}

Bytes string_element(std::uint32_t element_id, std::string_view value) {
    return element(element_id, Bytes(value.begin(), value.end()));
}

Bytes float_element(std::uint32_t element_id, double value) {
    Bytes payload;
    append_big_endian(payload, std::bit_cast<std::uint64_t>(value), 8);
    return element(element_id, payload);
}

Bytes concat(std::initializer_list<Bytes> parts) {
    Bytes out;
    for (const Bytes& part : parts) {
        out.insert(out.end(), part.begin(), part.end());
    }
    return out;
}

void write_bytes(std::ofstream& file, const Bytes& bytes) {
    file.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
}

}  // namespace

MatroskaVideoWriter::MatroskaVideoWriter(std::filesystem::path path, std::span<const std::uint8_t> codec_private,
                                         int width, int height, std::int64_t frame_duration_ns)
    : path_(std::move(path)), file_(path_, std::ios::binary | std::ios::trunc) {
    if (!file_) {
        throw std::runtime_error("cannot write " + path_.string());
    }
    const Bytes header = concat({uint_element(0x4286, 1), uint_element(0x42F7, 1), uint_element(0x42F2, 4),
                                 uint_element(0x42F3, 8), string_element(0x4282, "matroska"),
                                 uint_element(0x4287, 4), uint_element(0x4285, 2)});
    write_bytes(file_, element(0x1A45DFA3, header));
    write_bytes(file_, id(0x18538067));  // Segment, its size written by close()
    segment_size_at_ = file_.tellp();
    write_bytes(file_, {0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF});
    segment_start_ = file_.tellp();

    const Bytes info = concat({uint_element(0x2AD7B1, timestamp_scale_ns), string_element(0x4D80, app_name),
                               string_element(0x5741, app_name)});
    const Bytes info_element = element(0x1549A966, concat({info, float_element(0x4489, 0.0)}));
    // The Duration comes last in Info: written again by close().
    duration_at_ = segment_start_ + static_cast<std::streamoff>(info_element.size()) - 11;
    write_bytes(file_, info_element);

    const Bytes video = concat({uint_element(0xB0, static_cast<std::uint64_t>(width)),
                                uint_element(0xBA, static_cast<std::uint64_t>(height))});
    Bytes track = concat({uint_element(0xD7, 1), uint_element(0x73C5, 1), uint_element(0x83, 1),
                          uint_element(0x9C, 0), string_element(0x86, codec_id_vvc),
                          element(0x63A2, Bytes(codec_private.begin(), codec_private.end())), element(0xE0, video)});
    if (frame_duration_ns > 0) {
        const Bytes duration = uint_element(0x23E383, static_cast<std::uint64_t>(frame_duration_ns));
        track.insert(track.end(), duration.begin(), duration.end());
    }
    write_bytes(file_, element(0x1654AE6B, element(0xAE, track)));
}

MatroskaVideoWriter::~MatroskaVideoWriter() {
    if (!closed_) {
        abort();
    }
}

void MatroskaVideoWriter::write(std::span<const std::uint8_t> data, std::int64_t pts_ms, bool keyframe,
                                std::int64_t duration_ms) {
    if (pts_ms < 0) {
        throw std::invalid_argument("Matroska block timestamps must not be negative");
    }
    if (cluster_ts_ < 0 || keyframe || std::abs(pts_ms - cluster_ts_) > cluster_span_ms) {
        flush_cluster();
        cluster_ts_ = pts_ms;
    }
    const auto relative = static_cast<std::int16_t>(pts_ms - cluster_ts_);
    Bytes block = {0x81};  // track 1
    append_big_endian(block, static_cast<std::uint16_t>(relative), 2);
    block.push_back(keyframe ? 0x80 : 0x00);
    block.insert(block.end(), data.begin(), data.end());
    const Bytes simple_block = element(0xA3, block);
    cluster_.insert(cluster_.end(), simple_block.begin(), simple_block.end());
    last_end_ms_ = std::max(last_end_ms_, pts_ms + duration_ms);
    ++blocks_;
}

void MatroskaVideoWriter::flush_cluster() {
    if (cluster_ts_ < 0 || cluster_.empty()) {
        return;
    }
    Bytes body = uint_element(0xE7, static_cast<std::uint64_t>(cluster_ts_));
    body.insert(body.end(), cluster_.begin(), cluster_.end());
    write_bytes(file_, element(0x1F43B675, body));
    cluster_.clear();
}

void MatroskaVideoWriter::close() {
    if (closed_) {
        return;
    }
    flush_cluster();
    const std::streamoff end = file_.tellp();
    file_.seekp(segment_size_at_);
    Bytes segment_size = {0x01};
    append_big_endian(segment_size, static_cast<std::uint64_t>(end - segment_start_), 7);
    write_bytes(file_, segment_size);
    file_.seekp(duration_at_);
    write_bytes(file_, float_element(0x4489, static_cast<double>(last_end_ms_)));
    file_.close();
    closed_ = true;
    if (!file_) {
        throw std::runtime_error("could not finish " + path_.string());
    }
}

void MatroskaVideoWriter::abort() {
    if (file_.is_open()) {
        file_.close();
    }
    closed_ = true;
    std::error_code ignored;
    std::filesystem::remove(path_, ignored);
}

}  // namespace fgw
