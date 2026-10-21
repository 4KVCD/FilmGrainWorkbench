// Grain parameters that change over a video, one segment at a time: the
// result of the every-frame analysis, and the plan that tunes it.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

#include "fgw/grain_params.h"
#include "fgw/rational.h"
#include "fgw/tuning.h"

namespace fgw {

struct Segment {
    std::int64_t start = 0;  // first encoded frame (inclusive)
    std::int64_t end = 0;    // last encoded frame (exclusive)
    GrainParams params;
};

class TimelineModel {
public:
    TimelineModel() = default;
    TimelineModel(std::vector<Segment> segments, int bit_depth, int subsampling_x, int subsampling_y, Rational fps,
                  std::int64_t frames);

    const std::vector<Segment>& segments() const { return segments_; }
    // The segment frame `index` is in (the first one before them all).
    const Segment& segment_at(std::int64_t index) const;

    int bit_depth = 10;
    int subsampling_x = 1;
    int subsampling_y = 1;
    Rational fps{24, 1};
    std::int64_t frames = 0;
    double seconds = 0;            // how long the analysis took
    std::string kind = "every-frame";
    std::vector<std::int64_t> sigma_frames;  // the grain-over-time samples:
    std::vector<double> sigma_values;        // luma sigma at mid-grey, sample units
    std::vector<std::string> alignment;      // the preflight checks' verdicts
    std::vector<std::string> warnings;
    bool resumed = false;                    // the analysis continued an earlier one

    // The JSON the app saves analyses in (version 3, the Python app's format).
    std::string to_json() const;
    // Throws std::invalid_argument for anything this version cannot read.
    static TimelineModel from_json(std::string_view text);

    // Entries with real timestamps (100 ns units) for aomenc and SVT-AV1. As
    // in the written MKV, the whole video is covered: frames before the first
    // analysed one (a cut file's undecodable start) take the first segment,
    // frames after the last one the last.
    std::vector<TableEntry> grain_table() const;

private:
    std::vector<Segment> segments_;
    std::vector<std::int64_t> starts_;
};

// Every-frame parameters, scaled by the tuning on demand.
class TimelinePlan final : public GrainPlan {
public:
    TimelinePlan(std::shared_ptr<const TimelineModel> model, Tuning tuning);
    std::optional<GrainParams> params_at(std::int64_t index) const override;
    std::int64_t key_at(std::int64_t index) const override { return model_->segment_at(index).start; }
    bool clipped() const override;

private:
    std::shared_ptr<const TimelineModel> model_;
    Tuning tuning_;
    bool subsampled_420_;
    mutable std::mutex mutex_;
    mutable std::map<std::int64_t, GrainParams> cache_;  // by segment start
    mutable bool any_clipped_ = false;
};

}  // namespace fgw
