// The user's tuning (strength, flags, seed) applied to measured grain.
//
// Strength scales the grain's amplitude exactly: the scaling values are
// multiplied and the scaling shift re-chosen for headroom, so 200 % doubles
// the output noise at every brightness instead of saturating coded values at
// 255.
#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>

#include "fgw/grain_params.h"

namespace fgw {

struct Tuning {
    double luma_strength = 1.0;
    double chroma_strength = 1.0;
    bool overlap = true;                    // libaom's noise model: overlap_flag = 1
    bool clip_to_restricted_range = false;  // ...and clip_to_restricted_range = 0
    int seed = 7391;
    bool operator==(const Tuning&) const = default;
};

// Output noise standard deviation per 8-bit brightness, for luma, Cb and Cr.
struct Sigmas {
    std::array<double, 256> y{};
    std::array<double, 256> cb{};
    std::array<double, 256> cr{};
};

Sigmas entry_sigmas(const GrainParams& params, int bit_depth, int subsampling_x, int subsampling_y);

// A sigma in sample units of `bit_depth` as 8-bit units.
inline double sigma_in_8bit_units(double sigma, int bit_depth) { return sigma / (1 << (bit_depth - 8)); }

struct ScaledParams {
    GrainParams params;
    bool clipped = false;  // the strength asked for more than the format can code
};

// One parameter set's grain amplitude scaled by the tuning. For a fixed AR
// filter the output sigma is proportional to scaling / 2^scaling_shift, so
// multiplying the scaling values (and re-choosing the shift for headroom) is
// exact up to rounding.
ScaledParams scale_params(const GrainParams& params, const Tuning& tuning, bool subsampled_420 = true);

// What grain each picture gets. Thread-safe: the window asks on its thread,
// the frame loader on its workers.
class GrainPlan {
public:
    virtual ~GrainPlan() = default;
    // The parameters of picture `index`; nothing for a picture not known yet.
    virtual std::optional<GrainParams> params_at(std::int64_t index) const = 0;
    // Equal for pictures that get the same parameters (to reuse work done for them).
    virtual std::int64_t key_at(std::int64_t index) const = 0;
    // Whether any strength asked for so far was more than the format can code.
    virtual bool clipped() const { return false; }
};

// The same parameters for every picture.
class StaticPlan final : public GrainPlan {
public:
    explicit StaticPlan(GrainParams params, bool clipped = false) : params_(std::move(params)), clipped_(clipped) {}
    std::optional<GrainParams> params_at(std::int64_t) const override { return params_; }
    std::int64_t key_at(std::int64_t) const override { return 0; }
    bool clipped() const override { return clipped_; }

private:
    GrainParams params_;
    bool clipped_;
};

// Parameters estimated one frame at a time (the viewer's live estimates),
// scaled on demand.
class FramePlan final : public GrainPlan {
public:
    FramePlan(std::map<std::int64_t, GrainParams> estimates, Tuning tuning, bool subsampled_420);
    // The estimate of picture `index`, as measured (before the tuning).
    void add_estimate(std::int64_t index, GrainParams estimate);
    std::optional<GrainParams> params_at(std::int64_t index) const override;
    std::int64_t key_at(std::int64_t index) const override { return index; }
    // Whether the strength had to be capped for picture `index`.
    bool clipped_at(std::int64_t index) const;

private:
    std::optional<ScaledParams> scaled(std::int64_t index) const;

    std::map<std::int64_t, GrainParams> estimates_;
    Tuning tuning_;
    bool subsampled_420_;
    mutable std::mutex mutex_;
    mutable std::map<std::int64_t, ScaledParams> cache_;
};

}  // namespace fgw
