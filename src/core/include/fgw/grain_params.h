// AV1 film grain parameters and AOM's "filmgrn1" grain-table text format.
//
// Field meanings follow the AFGS1 / AV1 film grain semantics:
// - scaling points are (x, scaling) pairs on an 8-bit x axis (0..255);
// - AR coefficients are signed (-128..127, i.e. ArCoeffs*Plus128 - 128);
// - scaling_shift and ar_coeff_shift hold the actual shifts (8..11 and
//   6..9), not the *_minus8 / *_minus6 syntax values;
// - cb_mult, cb_luma_mult, cr_mult, cr_luma_mult (8 bits) and cb_offset,
//   cr_offset (9 bits) hold the coded values, so the effective multipliers
//   are value - 128 and the offsets value - 256.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace fgw {

inline constexpr int max_luma_points = 14;
inline constexpr int max_chroma_points = 10;

struct ScalingPoint {
    int x = 0;
    int scaling = 0;
    bool operator==(const ScalingPoint&) const = default;
};

// AR coefficients on the luma grain for a lag: those of the rows above and
// of the samples left of the one filtered.
constexpr int luma_ar_coefficient_count(int lag) { return 2 * lag * (lag + 1); }

struct GrainParams {
    bool apply_grain = true;
    int random_seed = 0;
    std::vector<ScalingPoint> y_points;
    std::vector<ScalingPoint> cb_points;
    std::vector<ScalingPoint> cr_points;
    bool chroma_scaling_from_luma = false;
    int scaling_shift = 8;
    int ar_coeff_lag = 0;
    std::vector<int> ar_coeffs_y;
    std::vector<int> ar_coeffs_cb;
    std::vector<int> ar_coeffs_cr;
    int ar_coeff_shift = 6;
    int grain_scale_shift = 0;
    int cb_mult = 128;
    int cb_luma_mult = 192;
    int cb_offset = 256;
    int cr_mult = 128;
    int cr_luma_mult = 192;
    int cr_offset = 256;
    bool overlap_flag = true;
    bool clip_to_restricted_range = false;

    bool operator==(const GrainParams&) const = default;

    // Chroma AR coefficients: the luma count, plus one for the luma grain
    // when there is luma grain.
    int chroma_ar_coefficient_count() const {
        return luma_ar_coefficient_count(ar_coeff_lag) + (y_points.empty() ? 0 : 1);
    }

    // Throws std::invalid_argument if the parameters cannot be coded or
    // synthesised. A set with apply_grain off is coded as that flag alone,
    // so its other fields do not matter: libaom leaves them all zero for a
    // frame it cannot measure (black frames).
    void validate(bool subsampled_420 = true) const;
};

// One "E" record: parameters valid for [start, end), in 100 ns units in
// libaom's tables (frame numbers in the grain analyzer's).
struct TableEntry {
    std::int64_t start = 0;
    std::int64_t end = 0;
    GrainParams params;
};

// Parses the text libaom's aom_film_grain_table_write writes. Throws
// std::invalid_argument for anything else.
std::vector<TableEntry> parse_grain_table(std::string_view text);

// Writes entries in libaom's layout (readable by aomenc and SVT-AV1).
std::string format_grain_table(const std::vector<TableEntry>& entries);

}  // namespace fgw
