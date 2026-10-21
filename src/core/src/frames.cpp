#include "fgw/frames.h"

#include <cmath>
#include <cstring>
#include <fstream>
#include <mutex>
#include <stdexcept>

#include "fgw/text.h"

namespace fgw {

Geometry geometry_of(const VideoInfo& info) {
    return {info.width, info.height, info.bit_depth, info.subsampling_x, info.subsampling_y};
}

std::optional<Crop> source_crop_for(const VideoInfo& source, const VideoInfo& encoded) {
    const double source_ratio = static_cast<double>(source.width) / source.height;
    const double encoded_ratio = static_cast<double>(encoded.width) / encoded.height;
    if (std::abs(source_ratio - encoded_ratio) < 0.01) {
        return std::nullopt;
    }
    if (encoded_ratio > source_ratio) {  // the encode is wider: crop the source's top and bottom
        const int height = static_cast<int>(std::nearbyint(source.width / encoded_ratio / 2)) * 2;
        return Crop{source.width, height, 0, (source.height - height) / 2 / 2 * 2};
    }
    const int width = static_cast<int>(std::nearbyint(source.height * encoded_ratio / 2)) * 2;
    return Crop{width, source.height, (source.width - width) / 2 / 2 * 2, 0};
}

std::string filter_chain(const VideoInfo& info, const Geometry& target, const std::optional<Crop>& crop) {
    std::string chain;
    int width = info.width;
    int height = info.height;
    if (crop) {
        chain = format("crop=%d:%d:%d:%d,", crop->width, crop->height, crop->x, crop->y);
        width = crop->width;
        height = crop->height;
    }
    if (width != target.width || height != target.height) {
        chain += format("scale=%d:%d:flags=spline+accurate_rnd+full_chroma_int,", target.width, target.height);
    }
    return chain + "format=" + target.pix_fmt();
}

std::vector<std::string> seek_args(const VideoInfo& info, std::int64_t index) {
    const double seconds = info.first_pts + (static_cast<double>(index) - 0.5) / info.fps.value() - info.start_time;
    return {"-seek_timestamp", "1", "-ss", format("%.6f", seconds)};
}

std::vector<std::string> ffmpeg_command(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index,
                                        std::int64_t count, const Geometry& target, const std::optional<Crop>& crop,
                                        const std::string& output, bool with_times) {
    // -fps_mode passthrough stops ffmpeg duplicating or dropping frames to
    // "fix" timestamps that are rounded to 1 ms (it did, and shifted every
    // later frame by one); -copyts keeps the original timestamps so each
    // frame's index is known.
    std::vector<std::string> command = {ffmpeg, "-hide_banner", "-v", with_times ? "info" : "error", "-nostdin",
                                        "-y", "-copyts"};
    for (std::string& arg : seek_args(info, index)) {
        command.push_back(std::move(arg));
    }
    for (const std::string& arg :
         {std::string("-i"), utf8(info.path), std::string("-map"), "0:" + std::to_string(info.stream_index),
          std::string("-frames:v"), std::to_string(count), std::string("-an"), std::string("-sn"), std::string("-dn"),
          std::string("-vf"), filter_chain(info, target, crop) + (with_times ? ",showinfo" : ""),
          std::string("-fps_mode"), std::string("passthrough"), std::string("-f"), std::string("rawvideo"),
          std::string("-pix_fmt"), target.pix_fmt(), output}) {
        command.push_back(arg);
    }
    return command;
}

std::optional<double> showinfo_time(std::string_view line) {
    const std::size_t parsed = line.find("Parsed_showinfo");
    if (parsed == std::string_view::npos) {
        return std::nullopt;
    }
    const std::size_t key = line.find(" pts_time:", parsed);
    if (key == std::string_view::npos) {
        return std::nullopt;
    }
    std::size_t start = key + 10;
    while (start < line.size() && line[start] == ' ') {
        ++start;
    }
    std::size_t end = start;
    while (end < line.size() && (std::isdigit(static_cast<unsigned char>(line[end])) || line[end] == '.' ||
                                 (end == start && line[end] == '-'))) {
        ++end;
    }
    if (end == start) {
        return std::nullopt;
    }
    return std::stod(std::string(line.substr(start, end - start)));
}

std::int64_t frame_index(const VideoInfo& info, double seconds) {
    return static_cast<std::int64_t>(std::nearbyint((seconds - info.first_pts) * info.fps.value()));
}

namespace {

// Reads exactly `bytes` of the program's output into `into`; false if it ended first.
bool read_exact(platform::ChildProcess& child, void* into, std::size_t bytes) {
    auto* at = static_cast<std::uint8_t*>(into);
    while (bytes > 0) {
        const std::size_t count = child.read(at, bytes);
        if (count == 0) {
            return false;
        }
        at += count;
        bytes -= count;
    }
    return true;
}

// Reads one plane of raw samples (1 or 2 bytes each, little-endian).
bool read_plane(platform::ChildProcess& child, Plane& plane, int sample_bytes, std::vector<std::uint8_t>& scratch) {
    if (sample_bytes == 2) {
        return read_exact(child, plane.samples.data(), plane.bytes());  // x64 is little-endian, as the data
    }
    scratch.resize(plane.samples.size());
    if (!read_exact(child, scratch.data(), scratch.size())) {
        return false;
    }
    std::copy(scratch.begin(), scratch.end(), plane.samples.begin());
    return true;
}

std::string file_name(const VideoInfo& info) { return utf8(info.path.filename()); }

}  // namespace

std::vector<Frame> read_frames(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index,
                               std::int64_t count, CancelToken* cancel, const std::optional<Geometry>& target,
                               const std::optional<Crop>& crop) {
    const Geometry geometry = target.value_or(geometry_of(info));
    if (cancel != nullptr) {
        cancel->check();
    }
    std::mutex times_mutex;
    std::vector<double> times;
    platform::ProcessOptions options;
    options.output_pipe_bytes = geometry.frame_bytes();
    options.on_error_line = [&times_mutex, &times](std::string_view line) {
        if (const std::optional<double> time = showinfo_time(line)) {
            const std::lock_guard lock(times_mutex);
            times.push_back(*time);
        }
    };
    platform::ChildProcess child = [&] {
        try {
            return platform::ChildProcess::start(
                ffmpeg_command(ffmpeg, info, index, count, geometry, crop, "pipe:1", true), options);
        } catch (const platform::ProcessError& error) {
            throw ToolError("Program not found: " + ffmpeg + " (" + error.what() + ")");
        }
    }();
    const CancelToken::Tracking tracking(cancel, child);
    std::vector<Frame> frames;
    std::vector<std::uint8_t> scratch;
    const std::atomic<bool> never = false;
    for (std::int64_t k = 0; k < count; ++k) {
        Frame frame = make_frame(geometry, index + k);
        child.gather(geometry.frame_bytes(), never);
        if (!read_plane(child, frame.y, geometry.sample_bytes(), scratch) ||
            !read_plane(child, frame.u, geometry.sample_bytes(), scratch) ||
            !read_plane(child, frame.v, geometry.sample_bytes(), scratch)) {
            break;
        }
        frames.push_back(std::move(frame));
    }
    const int exit_code = child.wait();
    const std::string log = child.standard_error();  // also waits for the last showinfo lines
    if (cancel != nullptr && cancel->cancelled()) {
        throw Cancelled();
    }
    if (exit_code != 0) {
        throw tool_failure(ffmpeg, exit_code, log);
    }
    if (frames.empty()) {
        throw std::runtime_error(file_name(info) + ": frame " + std::to_string(index) + " could not be decoded");
    }
    const std::lock_guard lock(times_mutex);
    for (std::size_t k = 0; k < frames.size() && k < times.size(); ++k) {
        frames[k].index = frame_index(info, times[k]);
    }
    return frames;
}

Frame read_frame(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index, CancelToken* cancel,
                 const std::optional<Geometry>& target, const std::optional<Crop>& crop) {
    return std::move(read_frames(ffmpeg, info, index, 1, cancel, target, crop).front());
}

std::int64_t extract_raw(const std::string& ffmpeg, const VideoInfo& info, std::int64_t index, std::int64_t count,
                         const std::filesystem::path& output, CancelToken* cancel, const std::optional<Geometry>& target,
                         const std::optional<Crop>& crop) {
    const Geometry geometry = target.value_or(geometry_of(info));
    run_tool(ffmpeg_command(ffmpeg, info, index, count, geometry, crop, utf8(output)), cancel);
    std::error_code error;
    const std::uintmax_t size = std::filesystem::file_size(output, error);
    return error ? 0 : static_cast<std::int64_t>(size / geometry.frame_bytes());
}

LumaStack luma_planes(const std::filesystem::path& path, const Geometry& geometry, std::int64_t count) {
    LumaStack stack{geometry.width, geometry.height, {}};
    for (std::int64_t k = 0; k < count; ++k) {
        const Frame frame = read_raw_frame(path, geometry, k, k);
        stack.planes.emplace_back(frame.y.samples.begin(), frame.y.samples.end());
    }
    return stack;
}

Frame read_raw_frame(const std::filesystem::path& path, const Geometry& geometry, std::int64_t frame,
                     std::int64_t index) {
    std::ifstream file(path, std::ios::binary);
    file.seekg(static_cast<std::streamoff>(frame) * static_cast<std::streamoff>(geometry.frame_bytes()));
    std::vector<std::uint8_t> data(geometry.frame_bytes());
    file.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(data.size()));
    if (!file) {
        throw std::runtime_error("frame " + std::to_string(frame) + " is not in " + utf8(path.filename()));
    }
    return frame_from_raw(data.data(), geometry, index);
}

}  // namespace fgw
