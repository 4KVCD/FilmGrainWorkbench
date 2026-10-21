#include "fgw/picture.h"

#include <cstring>
#include <stdexcept>

namespace fgw {

std::string Geometry::pix_fmt() const {
    std::string chroma;
    if (subsampling_x == 1 && subsampling_y == 1) {
        chroma = "420";
    } else if (subsampling_x == 1 && subsampling_y == 0) {
        chroma = "422";
    } else if (subsampling_x == 0 && subsampling_y == 0) {
        chroma = "444";
    } else {
        throw std::invalid_argument("no ffmpeg pixel format for this chroma subsampling");
    }
    return "yuv" + chroma + "p" + (bit_depth > 8 ? std::to_string(bit_depth) + "le" : "");
}

Frame make_frame(const Geometry& geometry, std::int64_t index) {
    Frame frame;
    frame.index = index;
    frame.geometry = geometry;
    frame.y = Plane(geometry.width, geometry.height);
    frame.u = Plane(geometry.chroma_width(), geometry.chroma_height());
    frame.v = Plane(geometry.chroma_width(), geometry.chroma_height());
    return frame;
}

namespace {

// Copies a plane from raw samples (1 or 2 bytes each, little-endian) and
// returns where the next plane starts.
const std::uint8_t* fill_plane(Plane& plane, const std::uint8_t* data, int sample_bytes) {
    const std::size_t count = plane.samples.size();
    if (sample_bytes == 2) {
        std::memcpy(plane.samples.data(), data, count * 2);  // x64 is little-endian, as the data
    } else {
        for (std::size_t i = 0; i < count; ++i) {
            plane.samples[i] = data[i];
        }
    }
    return data + count * static_cast<std::size_t>(sample_bytes);
}

}  // namespace

Frame frame_from_raw(const std::uint8_t* data, const Geometry& geometry, std::int64_t index) {
    Frame frame = make_frame(geometry, index);
    const int bytes = geometry.sample_bytes();
    data = fill_plane(frame.y, data, bytes);
    data = fill_plane(frame.u, data, bytes);
    fill_plane(frame.v, data, bytes);
    return frame;
}

}  // namespace fgw
