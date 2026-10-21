// YUV frames as 8-bit RGB for the screen.
//
// Every view (source, encode, encode + grain) goes through the same
// conversion, so flipping between them only ever shows the difference in the
// pictures. HDR (PQ, HLG) is tone-mapped to SDR for display; the files are
// not changed.
//
// Pixels are written as 0xFFRRGGBB words (Qt's QImage::Format_RGB32, the
// format it paints fastest), into memory the caller owns.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "fgw/picture.h"

namespace fgw {

struct DisplayFormat {
    int bit_depth = 8;
    bool limited_range = true;
    std::string matrix;     // ffprobe's color_space: bt709, bt2020nc, smpte170m...
    std::string transfer;   // ffprobe's color_transfer: smpte2084 (PQ), arib-std-b67 (HLG)...
    std::string primaries;  // ffprobe's color_primaries
    int width = 0;
    int height = 0;
    bool operator==(const DisplayFormat&) const = default;
};

// Where converted pixels go: `height` rows of `width` words, `stride` words apart.
struct RgbTarget {
    std::uint32_t* pixels = nullptr;
    std::ptrdiff_t stride = 0;
};

class Converter {
public:
    explicit Converter(DisplayFormat format);

    const DisplayFormat& format() const { return format_; }

    // Converts `frame` (its geometry's size) into `target`. Large frames are
    // converted in row bands on several threads; every pixel depends only on
    // its own samples, so the result is the same as in one piece.
    void to_rgb(const Frame& frame, RgbTarget target) const;

private:
    void convert_rows(const Frame& frame, int first_row, int end_row, RgbTarget target) const;

    DisplayFormat format_;
    bool hdr_ = false;
    bool bt2020_to_709_ = false;
    // The matrix as float constants, each worked out in double and rounded
    // once, as numpy did with Python floats next to float32 arrays.
    float kr_ = 0, kb_ = 0, kg_ = 0;
    float r_from_v_ = 0;  // 2 (1 - Kr)
    float b_from_u_ = 0;  // 2 (1 - Kb)
    // HDR: signal (0..1, in 4096 steps) to tone-mapped linear light, and
    // linear light to sRGB-encoded 8 bits.
    std::vector<float> to_linear_;
    std::vector<std::uint8_t> to_srgb_;
};

// A grey picture of the luma grain added to `clean`, amplified `gain` times
// (8-bit units), into `target`.
void grain_only(const Plane& clean, const Plane& grained, int bit_depth, RgbTarget target, float gain = 4.0f);

}  // namespace fgw
