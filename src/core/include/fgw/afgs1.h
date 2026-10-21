// AFGS1 film grain metadata carried as ITU-T T.35 registered user data.
//
// Implements metadata_aom_itu_t_t35() from the AOMedia AFGS1 specification
// (https://aomediacodec.github.io/afgs1-spec/): country code 0xB5, provider
// code 0x5890, provider-oriented code 0x01, then av1_film_grain_param_sets().
//
// The writer always codes one complete parameter set (update_grain_flag = 1):
// FFmpeg drops sets that only update the seed, so a self-contained set on
// every picture is the only form it applies.
//
// FFmpeg's parser also reads point_c*_value_increment_bits_minus1,
// point_c*_scaling_bits_minus5 and c*_scaling_offset when the point count is
// zero, which the specification does not. The writer therefore never codes an
// empty chroma curve: "no chroma grain" becomes one point with a scaling of 0,
// which both parsers read the same way and which adds no noise.
#pragma once

#include <array>
#include <cstdint>
#include <span>
#include <vector>

#include "fgw/grain_params.h"

namespace fgw {

// The four bytes every AFGS1 T.35 payload starts with.
inline constexpr std::array<std::uint8_t, 4> t35_prefix = {0xB5, 0x58, 0x90, 0x01};

struct PictureFormat {
    int width = 0;
    int height = 0;
    int bit_depth = 0;
    int subsampling_x = 1;
    int subsampling_y = 1;
    bool monochrome = false;
    bool operator==(const PictureFormat&) const = default;
};

// The complete T.35 payload (starting with country code 0xB5). Throws
// std::invalid_argument for parameters that cannot be coded.
std::vector<std::uint8_t> encode_t35(const GrainParams& params, const PictureFormat& format, int seed);

// Encodes the parameters once, then stamps a picture's seed into a copy:
// every picture needs its own grain seed or the grain freezes, and coding
// the ~150,000 pictures of a film bit by bit would be needlessly slow.
class T35Template {
public:
    T35Template(const GrainParams& params, const PictureFormat& format);
    std::vector<std::uint8_t> for_seed(int seed) const;

private:
    std::vector<std::uint8_t> payload_;
    bool apply_grain_;
};

inline constexpr int libaom_seed = 7391;
inline constexpr int libaom_seed_step = 3381;

// The grain seed of the picture shown at `picture_index`, as libaom's encoder
// picks it: the table seed (7391), plus 3381 after every shown frame (16-bit
// wrap), 0 replaced by 7391 (av1/encoder/encoder_utils.c). Used both to write
// and to preview, so the preview shows the grain a decoder produces.
int picture_seed(int base_seed, std::int64_t picture_index);

struct DecodedSet {
    GrainParams params;
    PictureFormat format;
    int seed = 0;
};

// Parses an AFGS1 T.35 payload strictly per the specification. Only
// self-contained sets (update_grain_flag = 1, no scaling prediction) are
// read, which is everything encode_t35 writes. Throws std::invalid_argument
// for anything else.
std::vector<DecodedSet> decode_t35(std::span<const std::uint8_t> data);

}  // namespace fgw
