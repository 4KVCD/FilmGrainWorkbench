// Writing an MKV whose VVC pictures each carry an AFGS1 prefix SEI:
//
// 1. Demux the encode's VVC packets (libavformat).
// 2. Insert one AFGS1 T.35 SEI per picture, with the picture's own seed, and
//    store the packets with their presentation times in a video-only MKV.
// 3. Let mkvmerge merge that track with every other track, chapter,
//    attachment and tag of the encode, copying the video track's colour and
//    HDR properties onto the new track.
// 4. Check the result has the same number of video packets and the same
//    leading timestamps as the encode.
#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

#include "fgw/afgs1.h"
#include "fgw/probe.h"
#include "fgw/tools.h"
#include "fgw/tuning.h"

namespace fgw {

struct WriteReport {
    std::filesystem::path output;
    std::int64_t pictures = 0;
    std::int64_t packets = 0;
    std::vector<std::string> notes;
    std::vector<std::string> copied;  // what else came over from the encode: "1 audio track", "12 chapters"
};

PictureFormat picture_format(const VideoInfo& info);

// Throws std::invalid_argument (a bad output name), ToolError, Cancelled, or
// std::runtime_error (the check of the written file failed).
WriteReport write_afgs1_mkv(const Tools& tools, const VideoInfo& encoded, const std::filesystem::path& output,
                            const GrainPlan& plan, int base_seed, const Progress& progress, CancelToken* cancel);

}  // namespace fgw
