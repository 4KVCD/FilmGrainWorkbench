// VVenC's film grain analysis added to a VVC encode made elsewhere.
//
// The app's VVenC (vvc/vvenc-fga-analysis-only.patch) analyses the source
// with --fga 4: the analyses of --fga 1, 2 and 3 on the same pictures,
// without coding them. Its output holds, after each analysis, each mode's
// parameter set as an H.274 film grain characteristics (FGC) SEI payload and
// the frame from which it applies. Those sets go into the user's encode as
// FGC SEI messages, one output file per mode, placed where VVenC places them
// in its own encodes: where the parameters change, at random access points,
// on the first picture written of a coded video sequence and on the picture
// after RASL pictures that carried one.
#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <set>
#include <span>
#include <vector>

#include "fgw/mux.h"
#include "fgw/probe.h"
#include "fgw/tools.h"
#include "fgw/vvc_grain.h"

namespace fgw {

// Each mode's FGC SEI payloads by the first frame each applies to, in
// frames of the analysed range (0 is its first); a set applies up to the
// next one's first frame, the first set also to the frames before it.
class FgaSegments {
public:
    // A set applies from `first_frame` on, replacing those from there on (as VVenC keeps them).
    void add(int mode, std::int64_t first_frame, std::vector<std::uint8_t> payload);
    bool has(int mode) const;
    // The payload in force at `frame`. Throws std::out_of_range for a mode without sets.
    const std::vector<std::uint8_t>& payload_at(int mode, std::int64_t frame) const;

private:
    std::map<std::int64_t, std::vector<std::uint8_t>> by_mode_[3];
};

// The sets vvencapp --fga 4 wrote. Throws std::invalid_argument for a
// stream without a set of every mode.
FgaSegments read_fga_segments(std::span<const std::uint8_t> stream);

// What analyse_film_grain found.
struct FgaAnalysis {
    std::int64_t first = 0;  // the encoded frame that is frame 0 of the segments
    std::int64_t count = 0;  // frames analysed
    FgaSegments segments;
};

// The grain of the source frames the encode's frames show: the source at
// `offset` frames from the encode, cropped and scaled to the encode's
// picture as the AV1 analysis reads it, analysed by vvencapp --fga 4 with
// `coding`'s QP, preset and strength (the QP sets how much the analysis's
// filter smooths, as in an encode at that QP) at the encode's bit depth and
// frame rate (vvenc_frame_rate), as VVenC analyses what it encodes.
// Throws std::invalid_argument for an encode VVenC cannot match (not 8- or
// 10-bit 4:2:0, a frame rate it does not take), ToolError, Cancelled.
FgaAnalysis analyse_film_grain(const Tools& tools, const VvcTools& vvc_tools, const VideoInfo& source,
                               const VideoInfo& encoded, std::int64_t offset, const std::optional<Crop>& crop,
                               const VvcCoding& coding, const std::filesystem::path& work_folder,
                               const Progress& progress, CancelToken* cancel);

// Where an FGC SEI goes, as VVenC decides it (EncGOP::xFilmGrainSeiNeeded),
// for pictures met in decoding order with their frame numbers (output order).
class FgcPlacement {
public:
    // Whether the picture needs the message: the parameters differ from the
    // previous frame's (`changed`), it is a random access point, no earlier
    // frame of its coded video sequence was written yet, or it follows RASL
    // pictures that carried one. `starts_sequence`: the picture follows an
    // end of sequence, so it starts a coded video sequence (an IDR always does).
    bool needed(std::int64_t frame, int picture_type, bool changed, bool starts_sequence = false);

private:
    std::set<std::int64_t> written_;  // frames of the coded video sequence written so far (the latest 160)
    bool rasl_pending_ = false;
};

// The encode as an MKV whose pictures carry mode `fga`'s parameters as FGC
// SEI messages (the encode's own FGC messages left out). Throws as write_vvc_mkv.
WriteReport write_fgc_mkv(const Tools& tools, const VideoInfo& encoded, const std::filesystem::path& output,
                          const FgaAnalysis& analysis, int fga, const Progress& progress, CancelToken* cancel);

}  // namespace fgw
