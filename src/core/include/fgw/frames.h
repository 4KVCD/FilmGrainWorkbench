// Decoding exact frames with ffmpeg into planes, or into raw planar files.
#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <string>
#include <vector>

#include "fgw/picture.h"
#include "fgw/probe.h"
#include "fgw/tools.h"

namespace fgw {

// The geometry of a video's own pictures.
Geometry geometry_of(const VideoInfo& info);

// The centre crop that gives the source the encode's aspect ratio
// (letterbox removal); nothing when they already match.
std::optional<Crop> source_crop_for(const VideoInfo& source, const VideoInfo& encoded);

// ffmpeg's -vf chain: crop, scale to `target`'s size, convert to its layout.
std::string filter_chain(const VideoInfo& info, const Geometry& target, const std::optional<Crop>& crop);

// Input options that make ffmpeg start exactly at frame `index`; use with
// -copyts. The seek goes to half a frame before the frame on the file's own
// timeline, and accurate seeking drops everything earlier. With -copyts
// ffmpeg moves that cut-off later by the file's start time, so it is taken
// off here: a cut encode starting at 0.626 s otherwise gave frame 506 for 491.
std::vector<std::string> seek_args(const VideoInfo& info, std::int64_t index);

// The ffmpeg command writing `count` frames from `index` as raw `target`
// frames to `output` ("pipe:1" for standard output). `with_times` adds
// showinfo, whose lines give each frame's timestamp.
std::vector<std::string> ffmpeg_command(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index,
                                        std::int64_t count, const Geometry& target, const std::optional<Crop>& crop,
                                        const std::string& output, bool with_times = false);

// The pts_time of a showinfo line ("Parsed_showinfo... pts_time:12.345"), if it is one.
std::optional<double> showinfo_time(std::string_view line);

// The index of the frame shown at `seconds` on the file's absolute timeline.
std::int64_t frame_index(const VideoInfo& info, double seconds);

// Decodes up to `count` frames from `index` (one seek). Each frame carries
// its true index, from its timestamp: near the start of a cut file the first
// frames may not be decodable, so the result can start later than `index`.
// `target` defaults to the video's own geometry. Throws ToolError, Cancelled,
// or std::runtime_error when no frame comes.
std::vector<Frame> read_frames(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index,
                               std::int64_t count, CancelToken* cancel = nullptr,
                               const std::optional<Geometry>& target = std::nullopt,
                               const std::optional<Crop>& crop = std::nullopt);

// read_frames for a caller that keeps only part of each frame: hands each
// frame to `each` as it arrives (`each` may move it away; the next one is
// then made anew), and returns the frames' true indices, in order, which
// are known only once ffmpeg has finished.
std::vector<std::int64_t> decode_frames(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index,
                                        std::int64_t count, const std::function<void(Frame&)>& each,
                                        CancelToken* cancel = nullptr,
                                        const std::optional<Geometry>& target = std::nullopt,
                                        const std::optional<Crop>& crop = std::nullopt);

Frame read_frame(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index, CancelToken* cancel = nullptr,
                 const std::optional<Geometry>& target = std::nullopt, const std::optional<Crop>& crop = std::nullopt);

// Writes `count` consecutive frames as raw planar YUV to `output`; returns
// how many were written.
std::int64_t extract_raw(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index, std::int64_t count,
                         const std::filesystem::path& output, CancelToken* cancel = nullptr,
                         const std::optional<Geometry>& target = std::nullopt,
                         const std::optional<Crop>& crop = std::nullopt);

// The luma planes of a raw file's first `count` frames, as floats.
struct LumaStack {
    int width = 0;
    int height = 0;
    std::vector<std::vector<float>> planes;
};
LumaStack luma_planes(const std::filesystem::path& path, const Geometry& geometry, std::int64_t count);

// One frame of a raw planar file, `frame` frames in.
Frame read_raw_frame(const std::filesystem::path& path, const Geometry& geometry, std::int64_t frame,
                     std::int64_t index);

}  // namespace fgw
