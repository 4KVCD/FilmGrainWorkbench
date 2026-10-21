#include "fgw/timeline.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include <nlohmann/json.hpp>

namespace fgw {

using json = nlohmann::ordered_json;  // keys in the order the Python app wrote them

namespace {

// Scaled parameters a TimelinePlan keeps. A whole-film Write meets ~150,000
// segments (libaom's model changes on almost every frame), ~0.8 KB each.
constexpr std::size_t cached_segments = 4096;

}  // namespace

TimelineModel::TimelineModel(std::vector<Segment> segments, int bit_depth_, int subsampling_x_, int subsampling_y_,
                             Rational fps_, std::int64_t frames_)
    : bit_depth(bit_depth_),
      subsampling_x(subsampling_x_),
      subsampling_y(subsampling_y_),
      fps(fps_),
      frames(frames_),
      segments_(std::move(segments)) {
    if (segments_.empty()) {
        throw std::invalid_argument("a grain timeline needs at least one segment");
    }
    std::stable_sort(segments_.begin(), segments_.end(), [](const Segment& a, const Segment& b) { return a.start < b.start; });
    starts_.reserve(segments_.size());
    for (const Segment& segment : segments_) {
        starts_.push_back(segment.start);
    }
}

const Segment& TimelineModel::segment_at(std::int64_t index) const {
    const auto after = std::upper_bound(starts_.begin(), starts_.end(), index);
    const auto i = std::max<std::ptrdiff_t>(0, (after - starts_.begin()) - 1);
    return segments_[static_cast<std::size_t>(i)];
}

namespace {

double rounded(double value, int decimals) {
    const double scale = std::pow(10.0, decimals);
    return std::nearbyint(value * scale) / scale;
}

}  // namespace

std::string TimelineModel::to_json() const {
    std::vector<TableEntry> entries;
    entries.reserve(segments_.size());
    for (const Segment& segment : segments_) {
        entries.push_back({segment.start, segment.end, segment.params});
    }
    std::vector<double> values;
    values.reserve(sigma_values.size());
    for (const double value : sigma_values) {
        values.push_back(rounded(value, 4));
    }
    json out;
    out["version"] = 3;
    out["kind"] = "timeline";
    out["mode"] = kind;
    out["bit_depth"] = bit_depth;
    out["subsampling"] = {subsampling_x, subsampling_y};
    out["fps"] = {fps.num, fps.den};
    out["frames"] = frames;
    out["seconds"] = rounded(seconds, 1);
    out["alignment"] = alignment;
    out["sigma_frames"] = sigma_frames;
    out["sigma_values"] = values;
    out["notes"] = {{"warnings", warnings}, {"resumed", resumed}};
    out["table_in_frames"] = format_grain_table(entries);
    return out.dump();
}

TimelineModel TimelineModel::from_json(std::string_view text) {
    const json d = json::parse(text, nullptr, false);
    // Version 2 models came from reads that could land on the wrong frame.
    if (d.is_discarded() || !d.is_object() || d.value("kind", "") != "timeline" || d.value("version", 0) != 3) {
        throw std::invalid_argument("not a grain model this version can read");
    }
    try {
        std::vector<Segment> segments;
        for (TableEntry& entry : parse_grain_table(d.at("table_in_frames").get<std::string>())) {
            segments.push_back({entry.start, entry.end, std::move(entry.params)});
        }
        const json& subsampling = d.at("subsampling");
        const json& fps = d.at("fps");
        TimelineModel model(std::move(segments), d.at("bit_depth").get<int>(), subsampling.at(0).get<int>(),
                            subsampling.at(1).get<int>(),
                            Rational{fps.at(0).get<std::int64_t>(), fps.at(1).get<std::int64_t>()},
                            d.at("frames").get<std::int64_t>());
        model.seconds = d.value("seconds", 0.0);
        model.kind = d.value("mode", "every-frame");
        model.sigma_frames = d.value("sigma_frames", std::vector<std::int64_t>{});
        model.sigma_values = d.value("sigma_values", std::vector<double>{});
        model.alignment = d.value("alignment", std::vector<std::string>{});
        const json notes = d.value("notes", json::object());
        model.warnings = notes.value("warnings", std::vector<std::string>{});
        model.resumed = notes.value("resumed", false);
        return model;
    } catch (const json::exception& error) {
        throw std::invalid_argument(std::string("a saved grain model could not be read: ") + error.what());
    }
}

std::vector<TableEntry> TimelineModel::grain_table() const {
    const auto timestamp = [this](std::int64_t frame) { return frame * 10'000'000 * fps.den / fps.num; };
    std::vector<TableEntry> entries;
    for (std::size_t i = 0; i < segments_.size(); ++i) {
        const Segment& segment = segments_[i];
        const bool first = i == 0;
        const bool last = i + 1 == segments_.size();
        entries.push_back({timestamp(first ? 0 : segment.start),
                           timestamp(last ? std::max(segment.end, frames) : segment.end), segment.params});
    }
    return entries;
}

TimelinePlan::TimelinePlan(std::shared_ptr<const TimelineModel> model, Tuning tuning)
    : model_(std::move(model)),
      tuning_(tuning),
      subsampled_420_(model_->subsampling_x == 1 && model_->subsampling_y == 1) {}

std::optional<GrainParams> TimelinePlan::params_at(std::int64_t index) const {
    const Segment& segment = model_->segment_at(index);
    const std::lock_guard lock(mutex_);
    auto cached = cache_.find(segment.start);
    if (cached == cache_.end()) {
        if (cache_.size() >= cached_segments) {
            cache_.clear();  // scaled again when met again: the same parameters
        }
        ScaledParams scaled = scale_params(segment.params, tuning_, subsampled_420_);
        any_clipped_ = any_clipped_ || scaled.clipped;
        cached = cache_.emplace(segment.start, std::move(scaled.params)).first;
    }
    return cached->second;
}

bool TimelinePlan::clipped() const {
    const std::lock_guard lock(mutex_);
    return any_clipped_;
}

}  // namespace fgw
