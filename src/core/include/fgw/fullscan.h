// Every-frame grain analysis.
//
// libaom's encoder (aomenc --denoise-noise-level) updates its noise model on
// every frame and derives that frame's film grain parameters from the
// running model, restarting it when the noise changes. This does the same
// for a (source, encode) pair:
//
//   ffmpeg (one process: both videos decoded once, in order, stacked)
//     -> afgs1_grain_analyzer (libaom's noise model on N threads)
//     -> a table of parameter segments (frame numbers)
//
// The analyzer saves the running model every checkpoint_every frames, so an
// interrupted run resumes exactly where it stopped.
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "fgw/grain_params.h"
#include "fgw/picture.h"
#include "fgw/probe.h"
#include "fgw/timeline.h"
#include "fgw/tools.h"

namespace fgw {

inline constexpr int checkpoint_every = 1000;  // frames between saved model states (~1 min of 4K work)

std::filesystem::path analyzer_path();
int default_threads();

// Rough wall time of the analysis: ~11 fps on 24 threads for 4K, decoding
// included (measured on The Beekeeper). The larger of the two pictures sets
// the pace.
double estimate_seconds(const VideoInfo& encoded, int threads, const VideoInfo* source = nullptr);

// The encoded frames that have a source frame at `offset`: [first, last).
// Throws std::invalid_argument when they do not overlap.
std::pair<std::int64_t, std::int64_t> frame_range(const VideoInfo& encoded, const VideoInfo& source,
                                                  std::int64_t offset);

// The name of a pair's analysis folder: the same key the Python app used, so
// its analyses (finished or stopped) are found again.
std::string cache_key(const VideoInfo& source, const VideoInfo& encoded, std::int64_t offset,
                      const std::optional<Crop>& crop);

// The next frame an analyzer checkpoint records; nothing if it is not one.
std::optional<std::int64_t> checkpoint_frame(const std::filesystem::path& path);

// Fills the model's grain-over-time samples (up to 2,000 points).
void sample_sigmas(TimelineModel& model, CancelToken* cancel);

// Analyses every frame of the pair, or reads back a finished analysis in
// `work_dir`, resuming a stopped one. Throws ToolError, Cancelled, or
// std::runtime_error (frames that do not line up).
TimelineModel analyze_every_frame(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded,
                                  std::int64_t offset, const std::optional<Crop>& crop,
                                  const std::filesystem::path& work_dir, const Progress& progress,
                                  CancelToken* cancel, int threads = 0, bool source_hwaccel = true,
                                  const std::filesystem::path& analyzer = {});

// The grain parameters of one frame: libaom's noise model started fresh on
// it, which is what the encoder derives for the first frame of a shot
// (~0.7 s for a 4K frame). apply_grain is off when the model finds no
// usable flat areas.
GrainParams estimate_frame(const Frame& source, const Frame& encoded, CancelToken* cancel = nullptr,
                           const std::filesystem::path& analyzer = {});

}  // namespace fgw
