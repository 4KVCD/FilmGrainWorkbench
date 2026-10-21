// Checking that source and encode frames are paired correctly, and finding
// the frame offset between them.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "fgw/picture.h"
#include "fgw/probe.h"
#include "fgw/tools.h"

namespace fgw {

// A luma plane as floats (a subsampled picture, a thumbnail).
struct LumaImage {
    int width = 0;
    int height = 0;
    std::vector<float> samples;
};

double psnr(const LumaImage& a, const LumaImage& b, double peak);

enum class Alignment { ok, shifted, unclear };
std::string to_string(Alignment alignment);  // "ok", "shifted", "unclear"

// For runs of source and encoded luma planes paired one to one: the mean
// matched PSNR, and whether some shift of up to `max_shift` frames matches
// clearly better ("shifted"), clearly worse ("ok"), or the same (static
// content: "unclear").
std::pair<double, Alignment> check_alignment(const std::vector<LumaImage>& source,
                                             const std::vector<LumaImage>& encoded, double peak, int max_shift = 3);

struct DetectedOffset {
    std::int64_t offset = 0;  // encoded frame k shows source frame k + offset
    double confidence = 0;    // 0..1
};

// Finds the frame offset by matching small luma thumbnails at three places.
DetectedOffset detect_offset(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded,
                             const std::optional<Crop>& crop, CancelToken* cancel = nullptr, int search = 48,
                             int positions = 3);

// Luma averaged over 8×8 blocks: grain averages out, the picture stays.
LumaImage block_means(const Plane& luma, int size = 8);

enum class MatchVerdict { same, neighbour, none, unclear, missing };

struct FrameMatch {
    MatchVerdict verdict = MatchVerdict::missing;
    std::optional<double> same_db;       // the paired source frame
    std::optional<double> neighbour_db;  // the best of the frames beside it
};

// Is `encoded` the source frame `paired`? `source` holds block means of the
// paired frame and whichever neighbours are at hand. Comparing block means,
// not pixels, keeps the grain the encode removed from reading as a mismatch;
// comparing with the neighbours tells a wrong offset from a still shot.
FrameMatch frame_match(const std::map<std::int64_t, LumaImage>& source, const LumaImage& encoded,
                       std::int64_t paired, double peak);

}  // namespace fgw
