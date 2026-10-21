// Pictures as the app holds them: planar YUV, every sample in 16 bits
// whatever the bit depth, so one code path serves 8-bit and 10-bit video.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace fgw {

// The planar layout frames are converted to (normally the encode's own).
struct Geometry {
    int width = 0;
    int height = 0;
    int bit_depth = 8;
    int subsampling_x = 1;
    int subsampling_y = 1;

    bool operator==(const Geometry&) const = default;

    int chroma_width() const { return (width + subsampling_x) >> subsampling_x; }
    int chroma_height() const { return (height + subsampling_y) >> subsampling_y; }
    // Bytes per sample in ffmpeg's raw output: 1 up to 8 bits, else 2 (little-endian).
    int sample_bytes() const { return bit_depth > 8 ? 2 : 1; }
    std::size_t frame_samples() const {
        return static_cast<std::size_t>(width) * height +
               2 * static_cast<std::size_t>(chroma_width()) * chroma_height();
    }
    std::size_t frame_bytes() const { return frame_samples() * static_cast<std::size_t>(sample_bytes()); }
    // ffmpeg's name for the layout: yuv420p, yuv420p10le, yuv444p12le...
    std::string pix_fmt() const;
};

// A crop of the source picture.
struct Crop {
    int width = 0;
    int height = 0;
    int x = 0;
    int y = 0;
    bool operator==(const Crop&) const = default;
};

// One plane: width × height samples, row after row.
struct Plane {
    int width = 0;
    int height = 0;
    std::vector<std::uint16_t> samples;

    Plane() = default;
    Plane(int w, int h) : width(w), height(h), samples(static_cast<std::size_t>(w) * h) {}

    std::uint16_t* row(int y) { return samples.data() + static_cast<std::size_t>(y) * width; }
    const std::uint16_t* row(int y) const { return samples.data() + static_cast<std::size_t>(y) * width; }
    std::uint16_t at(int x, int y) const { return row(y)[x]; }
    std::size_t bytes() const { return samples.size() * sizeof(std::uint16_t); }
};

// A decoded frame: its index in its video and its three planes.
struct Frame {
    std::int64_t index = 0;
    Geometry geometry;
    Plane y;
    Plane u;
    Plane v;

    std::size_t bytes() const { return y.bytes() + u.bytes() + v.bytes(); }
};

// An empty frame of `geometry`.
Frame make_frame(const Geometry& geometry, std::int64_t index = 0);

// A frame from ffmpeg's raw output in `geometry`'s layout (frame_bytes() long).
Frame frame_from_raw(const std::uint8_t* data, const Geometry& geometry, std::int64_t index);

}  // namespace fgw
