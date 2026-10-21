#include "fgw/picture.h"

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

}  // namespace fgw
