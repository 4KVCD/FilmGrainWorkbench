#include "fgw/tuning.h"

#include <algorithm>
#include <cmath>

#include "fgw/synth.h"

namespace fgw {

namespace {

std::array<double, 256> lut_sigma(const std::vector<ScalingPoint>& points, int scaling_shift, double template_std) {
    const std::array<int, 256> lut = scaling_lut(points);
    std::array<double, 256> sigma{};
    for (std::size_t i = 0; i < lut.size(); ++i) {
        sigma[i] = static_cast<double>(lut[i]) / (1 << scaling_shift) * template_std;
    }
    return sigma;
}

// Scaling points with their scaling multiplied (not yet rounded).
struct ScaledPoint {
    int x;
    double scaling;
};

std::vector<ScaledPoint> multiplied(const std::vector<ScalingPoint>& points, double strength) {
    std::vector<ScaledPoint> out;
    out.reserve(points.size());
    for (const ScalingPoint& point : points) {
        out.push_back({point.x, point.scaling * strength});
    }
    return out;
}

// The points coded with `factor`, or none when every scaling rounds to 0.
// Rounded half to even, as the Python app's round() did.
std::vector<ScalingPoint> quantised(const std::vector<ScaledPoint>& points, double factor) {
    std::vector<ScalingPoint> out;
    bool any = false;
    for (const ScaledPoint& point : points) {
        const int scaling = static_cast<int>(std::min(255.0, std::nearbyint(point.scaling * factor)));
        out.push_back({point.x, scaling});
        any = any || scaling != 0;
    }
    return any ? out : std::vector<ScalingPoint>{};
}

}  // namespace

Sigmas entry_sigmas(const GrainParams& p, int bit_depth, int subx, int suby) {
    const double luma_std = p.y_points.empty() ? 0.0 : luma_template_std(p, bit_depth);
    const auto [cb_std, cr_std] = chroma_template_std(p, bit_depth, subx, suby);
    Sigmas sigmas;
    sigmas.y = lut_sigma(p.y_points, p.scaling_shift, luma_std);
    const auto& cb_points = p.chroma_scaling_from_luma ? p.y_points : p.cb_points;
    const auto& cr_points = p.chroma_scaling_from_luma ? p.y_points : p.cr_points;
    sigmas.cb = lut_sigma(cb_points, p.scaling_shift, cb_std);
    sigmas.cr = lut_sigma(cr_points, p.scaling_shift, cr_std);
    return sigmas;
}

ScaledParams scale_params(const GrainParams& p, const Tuning& tuning, bool subsampled_420) {
    const std::vector<ScaledPoint> luma = multiplied(p.y_points, tuning.luma_strength);
    // Chroma scaled from luma becomes explicit chroma curves: the luma curve,
    // at the chroma strength, applied to the average luma alone (below). The
    // tune plot (entry_sigmas) shows it the same way.
    const std::vector<ScalingPoint>& cb_points = p.chroma_scaling_from_luma ? p.y_points : p.cb_points;
    const std::vector<ScalingPoint>& cr_points = p.chroma_scaling_from_luma ? p.y_points : p.cr_points;
    std::vector<ScaledPoint> cb = multiplied(cb_points, tuning.chroma_strength);
    std::vector<ScaledPoint> cr = multiplied(cr_points, tuning.chroma_strength);
    double peak = 0.0;
    for (const std::vector<ScaledPoint>* points : {&luma, &std::as_const(cb), &std::as_const(cr)}) {
        for (const ScaledPoint& point : *points) {
            peak = std::max(peak, point.scaling);
        }
    }
    // The largest shift (finest steps) whose scaled peak still fits in 8 bits.
    int shift = 11;
    while (shift > 8 && peak * (1 << shift) / (1 << p.scaling_shift) > 255) {
        --shift;
    }
    const double factor = static_cast<double>(1 << shift) / (1 << p.scaling_shift);
    ScaledParams result;
    result.clipped = peak * factor > 255.5;

    std::vector<ScalingPoint> luma_q = quantised(luma, factor);
    std::vector<ScalingPoint> cb_q = quantised(cb, factor);
    std::vector<ScalingPoint> cr_q = quantised(cr, factor);
    if (subsampled_420 && cb_q.empty() != cr_q.empty()) {
        // 4:2:0 grain needs both chroma curves or neither.
        if (cb_q.empty()) cb_q = {{0, 0}};
        if (cr_q.empty()) cr_q = {{0, 0}};
    }
    GrainParams& scaled = result.params;
    scaled = p;
    scaled.y_points = luma_q;
    scaled.cb_points = cb_q;
    scaled.cr_points = cr_q;
    if (p.chroma_scaling_from_luma) {
        // The chroma scaling input is then the average luma alone, as with the
        // flag: (luma × (192 − 128) + chroma × (128 − 128)) >> 6, offset 256 − 256.
        scaled.cb_mult = scaled.cr_mult = 128;
        scaled.cb_luma_mult = scaled.cr_luma_mult = 192;
        scaled.cb_offset = scaled.cr_offset = 256;
    }
    scaled.chroma_scaling_from_luma = false;
    scaled.scaling_shift = shift;
    scaled.overlap_flag = tuning.overlap;
    scaled.clip_to_restricted_range = tuning.clip_to_restricted_range;
    scaled.random_seed = tuning.seed & 0xFFFF;
    if (luma_q.empty()) {
        const auto luma_count = static_cast<std::size_t>(luma_ar_coefficient_count(p.ar_coeff_lag));
        scaled.ar_coeffs_y.clear();
        scaled.ar_coeffs_cb.resize(std::min(scaled.ar_coeffs_cb.size(), luma_count));
        scaled.ar_coeffs_cr.resize(std::min(scaled.ar_coeffs_cr.size(), luma_count));
    }
    scaled.apply_grain = p.apply_grain && !(luma_q.empty() && cb_q.empty() && cr_q.empty());
    return result;
}

FramePlan::FramePlan(std::map<std::int64_t, GrainParams> estimates, Tuning tuning, bool subsampled_420)
    : estimates_(std::move(estimates)), tuning_(tuning), subsampled_420_(subsampled_420) {}

void FramePlan::add_estimate(std::int64_t index, GrainParams estimate) {
    const std::lock_guard lock(mutex_);
    estimates_[index] = std::move(estimate);
    cache_.erase(index);
}

std::optional<ScaledParams> FramePlan::scaled(std::int64_t index) const {
    const std::lock_guard lock(mutex_);
    const auto estimate = estimates_.find(index);
    if (estimate == estimates_.end()) {
        return std::nullopt;
    }
    auto cached = cache_.find(index);
    if (cached == cache_.end()) {
        cached = cache_.emplace(index, scale_params(estimate->second, tuning_, subsampled_420_)).first;
    }
    return cached->second;
}

std::optional<GrainParams> FramePlan::params_at(std::int64_t index) const {
    std::optional<ScaledParams> scaled_params = scaled(index);
    if (!scaled_params) {
        return std::nullopt;
    }
    return std::move(scaled_params->params);
}

bool FramePlan::clipped_at(std::int64_t index) const {
    const std::optional<ScaledParams> scaled_params = scaled(index);
    return scaled_params && scaled_params->clipped;
}

}  // namespace fgw
