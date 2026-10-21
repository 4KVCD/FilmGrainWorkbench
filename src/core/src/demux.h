// Reading one stream's packets from a file with libavformat.
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <span>
#include <vector>

#include "fgw/rational.h"

struct AVFormatContext;
struct AVPacket;

namespace fgw {

struct DemuxedPacket {
    std::span<const std::uint8_t> data;  // valid until the next read
    std::optional<std::int64_t> pts;     // in the stream's time base
    std::int64_t duration = 0;
    bool keyframe = false;
};

class Demuxer {
public:
    // Opens `path` for the packets of stream `stream_index`. Throws
    // std::runtime_error if it cannot be read.
    Demuxer(const std::filesystem::path& path, int stream_index);
    ~Demuxer();
    Demuxer(const Demuxer&) = delete;
    Demuxer& operator=(const Demuxer&) = delete;

    Rational time_base() const;
    std::span<const std::uint8_t> extradata() const;
    // The stream's next packet with data; nothing at the end.
    std::optional<DemuxedPacket> next();

private:
    AVFormatContext* format_ = nullptr;
    AVPacket* packet_ = nullptr;
    int stream_index_;
};

}  // namespace fgw
