// A video stream's properties from ffprobe, including the true first
// presentation time.
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

#include "fgw/rational.h"
#include "fgw/tools.h"

namespace fgw {

struct VideoInfo {
    std::filesystem::path path;
    int stream_index = 0;
    std::string codec;
    int width = 0;
    int height = 0;
    Rational fps;
    std::string pix_fmt;
    int bit_depth = 8;
    int subsampling_x = 1;
    int subsampling_y = 1;
    double duration = 0;    // seconds
    double first_pts = 0;   // seconds: the earliest presentation time, not the first packet's
    std::string color_range;
    std::string matrix;     // ffprobe's color_space
    std::string transfer;   // ffprobe's color_transfer
    std::string primaries;  // ffprobe's color_primaries
    double start_time = 0;  // the container's start time
    std::int64_t frames = 0;  // the muxer's picture count, 0 when unknown

    bool operator==(const VideoInfo&) const = default;

    // Pictures in the stream: the muxer's count when known, else duration ×
    // fps. (The container's duration is often the audio's, which can run
    // past the video: a cut encode reported 2,145 frames for 2,128 pictures.)
    std::int64_t frame_count() const;
    bool limited_range() const { return color_range != "pc"; }
    bool is_hdr() const { return transfer == "smpte2084" || transfer == "arib-std-b67"; }
    // Content time (seconds from the first shown frame) of frame `index`.
    double time_of(std::int64_t index) const { return static_cast<double>(index) / fps.value(); }
    // "HEVC 3840×2160 23.976 fps, 10-bit yuv420p10le HDR, 1:57:41.33 (169,302 frames)"
    std::string describe() const;
};

// The earliest time in ffprobe's "packet=pts_time" CSV ("N/A" skipped).
std::optional<double> earliest_time(std::string_view csv);

// The VideoInfo of `path` from ffprobe's -show_streams -show_format JSON and
// the first presentation time. Throws ToolError for a video the app cannot use.
VideoInfo parse_probe(const std::filesystem::path& path, const nlohmann::json& data, double first_pts);

// Runs ffprobe on `path`. Throws ToolError, or Cancelled.
VideoInfo probe(const std::filesystem::path& path, const std::string& ffprobe, CancelToken* cancel = nullptr);

}  // namespace fgw
