// The AV1 tab's frames: which frame is shown, decoding the encode and the
// source, and the grain preview on top.
//
// A frame loads in two steps: the decoded encode (and its source frame)
// first, then the grain, which may need a live estimate (~0.8 s at 4K).
// Decoding, converting and synthesising all run on the frame job's thread.
#include <algorithm>
#include <future>

#include "av1_tab.h"
#include "fgw/afgs1.h"
#include "fgw/alignment.h"
#include "fgw/frames.h"
#include "fgw/fullscan.h"
#include "fgw/synth.h"
#include "fgw/text.h"

namespace fgw::app {

namespace {

constexpr int prefetch = 4;                               // frames decoded per seek while browsing
constexpr std::size_t cache_bytes = 800ull * 1024 * 1024;  // decoded frames kept for stepping back and forth

QString qstring(const std::string& text) { return QString::fromStdString(text); }
QString thousands(std::int64_t value) { return qstring(with_thousands(value)); }

// The encoded frame with grain added, and the grain alone.
std::map<View, QImage> grain_images(const Converter& converter, const Frame& encoded, const GrainParams& params, int seed) {
    Frame grained = encoded;
    add_grain(grained, params, seed);
    return {{grain_view, to_qimage(converter, grained)},
            {grain_only_view, grain_only_image(encoded.y, grained.y, encoded.geometry.bit_depth)}};
}

}  // namespace

int Av1Tab::frames_per_second() const {
    return encoded_ ? static_cast<int>(std::lround(encoded_->fps.value())) : 24;
}

void Av1Tab::step(int delta) {
    if (encoded_) {
        go_to(current_index_ + delta);
    }
}

void Av1Tab::go_to(std::int64_t index, bool force) {
    if (!encoded_) {
        return;
    }
    index = std::clamp<std::int64_t>(index, 0, encoded_->frame_count() - 1);
    if (index == current_index_ && !force && !state_->images().empty()) {
        return;
    }
    current_index_ = index;
    pane_->show_frame(index, encoded_->time_of(index));
    grain_timeline_->set_current(index);
    frame_timer_.start();
}

QString Av1Tab::caption(std::int64_t index) const {
    return "frame " + thousands(index) + " · " + timecode(encoded_->time_of(index));
}

std::shared_ptr<const Frame> Av1Tab::source_frame_of(std::int64_t index) {
    if (!source_) {
        return nullptr;
    }
    return frame_cache_.get({true, utf8(source_->path), index + offset_spin_->value(), crop_});
}

// Grain parameters exist for the frame, or can be estimated from its decoded source frame.
bool Av1Tab::grain_possible(std::int64_t index) {
    if (!plan_) {
        return false;
    }
    return plan_->params_at(index).has_value() || (model_ == nullptr && source_frame_of(index) != nullptr);
}

void Av1Tab::request_frame(std::int64_t index) {
    if (!encoded_ || !tools_.missing({"ffmpeg"}).empty()) {
        return;
    }
    if (running(frame_job_)) {
        pending_index_ = index;
        return;
    }
    pending_index_.reset();
    const std::optional<VideoInfo> source = source_;
    const VideoInfo encoded = *encoded_;
    const std::optional<Crop> crop = crop_;
    const std::string ffmpeg = tools_.ffmpeg;
    const std::int64_t offset = offset_spin_->value();
    const Geometry geometry = geometry_of(encoded);
    const std::string source_path = source ? utf8(source->path) : std::string();
    const std::string encoded_path = utf8(encoded.path);
    const std::shared_ptr<const Frame> source_frame =
        source ? frame_cache_.get({true, source_path, index + offset, crop}) : nullptr;
    const std::shared_ptr<const Frame> encoded_frame = frame_cache_.get({false, encoded_path, index, {}});
    // The source frames either side of the paired one tell a wrong offset from a still shot.
    std::map<std::int64_t, std::shared_ptr<const Frame>> beside;
    if (source) {
        for (const std::int64_t k : {index + offset - 1, index + offset + 1}) {
            if (auto frame = frame_cache_.get({true, source_path, k, crop})) {
                beside[k] = std::move(frame);
            }
        }
    }
    const std::optional<GrainParams> params = plan_ ? plan_->params_at(index) : std::nullopt;
    const int seed = picture_seed(seed_spin_->value(), index);
    const std::shared_ptr<const Converter> converter = converter_;
    // A seek into a long-GOP stream costs ~1-2 s at 4K, the next few frames
    // almost nothing: decode a small window in the direction of travel.
    const bool backwards = index < last_requested_;
    last_requested_ = index;
    const std::int64_t start = backwards ? std::max<std::int64_t>(0, index - prefetch + 1) : index;
    const std::int64_t count = std::min<std::int64_t>(prefetch, encoded.frame_count() - start);

    using Frames = std::vector<std::shared_ptr<const Frame>>;
    struct Loaded {
        Frames source_run;
        Frames encoded_run;
        std::map<View, QImage> images;
        std::optional<FrameMatch> match;
    };
    auto* job = new Job("Frame", [=, this](CancelToken& cancel, const Progress&) -> Job::Deliver {
        const auto shared = [](std::vector<Frame> frames) {
            Frames out;
            for (Frame& frame : frames) {
                out.push_back(std::make_shared<const Frame>(std::move(frame)));
            }
            return out;
        };
        // Both videos at once: each seek costs a second or two at 4K.
        const bool need_source = source && !source_frame && index + offset >= 0 && index + offset < source->frame_count();
        const std::int64_t source_start = std::max<std::int64_t>(0, start + offset - 1);  // one extra frame either side
        std::future<Frames> source_future;
        if (need_source) {
            const std::int64_t source_count = std::min(count + 2, source->frame_count() - source_start);
            source_future = std::async(std::launch::async, [&, source_count] {
                return shared(read_frames(ffmpeg, *source, source_start, source_count, &cancel, geometry, crop));
            });
        }
        auto loaded = std::make_shared<Loaded>();
        if (!encoded_frame) {
            loaded->encoded_run = shared(read_frames(ffmpeg, encoded, start, count, &cancel));
        }
        if (source_future.valid()) {
            loaded->source_run = source_future.get();
        }
        const auto find = [](const Frames& run, std::int64_t wanted) -> std::shared_ptr<const Frame> {
            const auto found = std::find_if(run.begin(), run.end(), [wanted](const auto& f) { return f->index == wanted; });
            return found == run.end() ? nullptr : *found;
        };
        const std::shared_ptr<const Frame> s = source_frame ? source_frame : find(loaded->source_run, index + offset);
        const std::shared_ptr<const Frame> e = encoded_frame ? encoded_frame : find(loaded->encoded_run, index);
        if (!e) {
            if (!loaded->encoded_run.empty() && loaded->encoded_run.front()->index > index) {
                // A file cut from a longer one starts with pictures that refer to
                // frames cut away; the first ones cannot be decoded.
                throw std::runtime_error("frame " + with_thousands(index) +
                                         " cannot be decoded: its pictures refer to frames cut away before it. The first "
                                         "frame that can is " + with_thousands(loaded->encoded_run.front()->index) + ".");
            }
            throw std::runtime_error("frame " + with_thousands(index) + " is past the end of the encode");
        }
        auto encoded_image = std::async(std::launch::async, [&] { return to_qimage(*converter, *e); });
        std::future<QImage> source_image;
        if (s) {
            source_image = std::async(std::launch::async, [&] { return to_qimage(*converter, *s); });
        }
        if (params) {
            loaded->images = grain_images(*converter, *e, *params, seed);
        }
        loaded->images[encoded_view] = encoded_image.get();
        if (source_image.valid()) {
            loaded->images[source_view] = source_image.get();
        }
        if (source) {
            const std::int64_t paired = index + offset;
            std::map<std::int64_t, std::shared_ptr<const Frame>> near = beside;
            for (const auto& frame : loaded->source_run) {
                near[frame->index] = frame;
            }
            if (s) {
                near[paired] = s;
            }
            std::map<std::int64_t, LumaImage> means;
            for (const std::int64_t k : {paired - 1, paired, paired + 1}) {
                if (const auto found = near.find(k); found != near.end()) {
                    means[k] = block_means(found->second->y);
                }
            }
            loaded->match = frame_match(means, block_means(e->y), paired, (1 << geometry.bit_depth) - 1);
        }
        return [this, loaded, index, offset, crop, source_path, encoded_path] {
            for (const auto& frame : loaded->source_run) {
                frame_cache_.put({true, source_path, frame->index, crop}, frame);
            }
            for (const auto& frame : loaded->encoded_run) {
                frame_cache_.put({false, encoded_path, frame->index, {}}, frame);
            }
            frame_cache_.trim(cache_bytes);
            if (index != current_index_) {
                return;
            }
            if (offset == offset_spin_->value() && loaded->match) {
                show_match(index, *loaded->match);
            }
            // A new frame's grain comes in a second step, so the frame itself shows at once.
            const bool grain_follows = !loaded->images.contains(grain_view) && grain_possible(index);
            grain_dirty_ = grain_dirty_ || grain_follows;
            const bool comparing_grain = state_->mode() == grain_view || state_->mode() == grain_only_view;
            if (grain_follows && comparing_grain && !state_->images().empty()) {
                // Someone comparing grain keeps seeing grain: the new frame appears with its grain.
                held_ = std::pair{index, loaded->images};
            } else {
                held_.reset();
                state_->set_images(loaded->images, caption(index));
            }
            if (!busy()) {
                status_->set_text(grain_follows ? "Frame " + thousands(index) + ": adding the estimated grain…"
                                                : "Frame " + thousands(index) + " loaded.");
            }
            update_segment_plot();
            update_enabled();
        };
    }, this);
    connect(job, &Job::failed, this, [this, index](const QString& message) {
        log(QString("Could not load frame %1: %2").arg(index).arg(message));
        if (index == current_index_) {
            state_->clear("Could not decode frame " + thousands(index) + ".\n\n" + message.section('\n', 0, 0));
        }
    });
    connect(job, &Job::ended, this, &Av1Tab::frame_job_ended);
    frame_job_ = job;
    if (state_->images().empty()) {
        state_->clear("Loading frame " + thousands(index) + "…");
    }
    if (!busy()) {
        status_->set_text("Loading frame " + thousands(index) + "…");
    }
    job->start();
}

// Under the offset: does the frame shown pair with the right source frame?
void Av1Tab::show_match(std::int64_t index, const FrameMatch& match) {
    QString text;
    if (source_) {
        const std::int64_t paired = index + offset_spin_->value();
        const double same = match.same_db.value_or(0.0);
        const double best = match.neighbour_db.value_or(0.0);
        switch (match.verdict) {
            case MatchVerdict::same:
                text = QString("✔ Frame %1 matches source frame %2 (%3 dB; the frames beside it %4 dB).")
                           .arg(thousands(index), thousands(paired)).arg(same, 0, 'f', 1).arg(best, 0, 'f', 1);
                break;
            case MatchVerdict::neighbour:
                text = QString("⚠ Frame %1: a source frame next to %2 matches it better (%3 vs %4 dB), so the offset is "
                               "off by one. Try Detect.")
                           .arg(thousands(index), thousands(paired)).arg(best, 0, 'f', 1).arg(same, 0, 'f', 1);
                break;
            case MatchVerdict::none:
                text = QString("⚠ Frame %1 does not match source frame %2 or the frames beside it (%3 dB): the frame "
                               "offset looks wrong. Try Detect.")
                           .arg(thousands(index), thousands(paired)).arg(same, 0, 'f', 1);
                break;
            case MatchVerdict::unclear:
                text = QString("Frame %1: the offset can't be checked here; the source frames around %2 look alike "
                               "(%3 dB).").arg(thousands(index), thousands(paired)).arg(same, 0, 'f', 1);
                break;
            case MatchVerdict::missing:
                text = QString("No source frame for encoded frame %1 at this offset (source frame %2).")
                           .arg(thousands(index), thousands(paired));
                break;
        }
    }
    QStringList lines;
    for (const QString& line : {detected_, text}) {
        if (!line.isEmpty()) {
            lines << line;
        }
    }
    offset_note_->setText(lines.join("\n"));
}

void Av1Tab::frame_job_ended() {
    if (pending_index_) {
        request_frame(*pending_index_);
    } else if (grain_dirty_) {
        render_grain();
    }
}

// Synthesises the grain views of the current frame again (after a tuning
// change, or as the second step of loading it).
void Av1Tab::render_grain() {
    if (!plan_ || !encoded_) {
        return;
    }
    if (running(frame_job_)) {
        grain_dirty_ = true;
        return;
    }
    grain_dirty_ = false;
    const std::shared_ptr<const Frame> encoded_frame = frame_cache_.get({false, utf8(encoded_->path), current_index_, {}});
    if (!encoded_frame) {
        request_frame(current_index_);
        return;
    }
    const std::int64_t index = current_index_;
    const std::int64_t offset = offset_spin_->value();
    const std::optional<GrainParams> params = plan_->params_at(index);
    const std::shared_ptr<const Frame> source_frame = source_frame_of(index);
    if (!params && (model_ != nullptr || !source_frame)) {
        request_frame(index);
        return;
    }
    const Tuning tune = tuning();
    const int seed = picture_seed(seed_spin_->value(), index);
    const bool subsampled_420 = encoded_->subsampling_x == 1 && encoded_->subsampling_y == 1;
    const std::shared_ptr<const Converter> converter = converter_;
    const auto deliver = [this, index, offset](std::map<View, QImage> images, std::optional<GrainParams> estimate) {
        if (estimate && offset == offset_spin_->value() && model_ == nullptr) {
            estimates_[index] = *estimate;
            if (frame_plan_) {
                frame_plan_->add_estimate(index, *estimate);
            }
        }
        std::optional<std::pair<std::int64_t, std::map<View, QImage>>> held = std::exchange(held_, std::nullopt);
        if (index != current_index_) {
            return;
        }
        if (held && held->first == index) {
            std::map<View, QImage> all = held->second;
            for (auto& [view, image] : images) {
                all[view] = image;
            }
            state_->set_images(all, caption(index));
        } else {
            state_->update_images(images);
        }
        if (!busy()) {
            status_->set_text("Frame " + thousands(index) + " loaded.");
        }
        update_segment_plot();
        update_enabled();
    };
    auto* job = new Job("Grain preview", [=](CancelToken& cancel, const Progress&) -> Job::Deliver {
        std::optional<GrainParams> estimate;
        if (!params) {  // the grain of this frame: libaom's model on it (~0.8 s at 4K)
            estimate = estimate_frame(*source_frame, *encoded_frame, &cancel);
        }
        const GrainParams shown = params ? *params : scale_params(*estimate, tune, subsampled_420).params;
        std::map<View, QImage> images = grain_images(*converter, *encoded_frame, shown, seed);
        return [deliver, images = std::move(images), estimate] { deliver(images, estimate); };
    }, this);
    connect(job, &Job::failed, this, [this, deliver](const QString& message) {
        log("Grain preview failed: " + message);
        deliver({}, std::nullopt);
    });
    connect(job, &Job::ended, this, &Av1Tab::frame_job_ended);
    frame_job_ = job;
    job->start();
}

}  // namespace fgw::app
