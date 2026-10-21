// AFGS1 / AV1 film grain synthesis (AFGS1 specification, section 8.2).
//
// Every rounding and clipping step is the specification's, so the output is
// bit-identical to FFmpeg's decoder. The noise is made and added one stripe
// of 32 picture rows at a time, so a 4K frame never needs full-size noise
// planes (about 50 MB).
#pragma once

#include <array>
#include <utility>
#include <vector>

#include "fgw/grain_params.h"
#include "fgw/picture.h"

namespace fgw {

// The grain templates of spec 8.2.3: white Gaussian noise shaped by the
// auto-regressive filter.
struct GrainTemplates {
    static constexpr int luma_height = 73;
    static constexpr int luma_width = 82;
    std::vector<int> luma;  // luma_height × luma_width, row after row
    int chroma_height = 0;
    int chroma_width = 0;
    std::vector<int> cb;  // chroma_height × chroma_width
    std::vector<int> cr;
};

GrainTemplates generate_grain(const GrainParams& params, int seed, int bit_depth, int subsampling_x,
                              int subsampling_y);

// Spec 8.2.4: the 256-entry piecewise-linear scaling table.
std::array<int, 256> scaling_lut(const std::vector<ScalingPoint>& points);

// Adds film grain to `frame` (spec 8.2), in place, with `seed` (the
// picture's own). `matrix_identity`: the video is RGB coded as YUV, whose
// chroma is clipped like luma.
void add_grain(Frame& frame, const GrainParams& params, int seed, bool matrix_identity = false);

// Standard deviation of the luma grain template in sample units: the
// synthesised luma noise at intensity I has a standard deviation of about
// scaling(I) / 2^scaling_shift × this. Averaged over three seeds.
double luma_template_std(const GrainParams& params, int bit_depth);

// The same for Cb and Cr.
std::pair<double, double> chroma_template_std(const GrainParams& params, int bit_depth, int subsampling_x,
                                              int subsampling_y);

}  // namespace fgw
