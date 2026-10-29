// Writing an MKV whose VVC pictures carry grain metadata as prefix SEIs
// (AFGS1, or VVC's own film grain characteristics):
//
// 1. Demux the encode's VVC packets (libavformat).
// 2. Edit each packet (for AFGS1: one T.35 SEI per picture, with the
//    picture's own seed), and store the packets with their presentation
//    times in a video-only MKV.
// 3. Let mkvmerge merge that track with every other track, chapter,
//    attachment and tag of the encode, copying the video track's colour and
//    HDR properties onto the new track.
// 4. Check the result has the same number of video packets and the same
//    leading timestamps as the encode.
#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <span>
#include <string>
#include <vector>

#include "fgw/afgs1.h"
#include "fgw/probe.h"
#include "fgw/tools.h"
#include "fgw/tuning.h"
#include "fgw/vvc.h"

namespace fgw {

struct WriteReport {
    std::filesystem::path output;
    std::int64_t pictures = 0;
    std::int64_t packets = 0;
    std::vector<std::string> notes;
    std::vector<std::string> copied;  // what else came over from the encode: "1 audio track", "12 chapters"
};

PictureFormat picture_format(const VideoInfo& info);

// Throws (with the reason) unless write_afgs1_mkv can read the encode's VVC
// track: its packets need a vvcC record, which Matroska and MP4 carry and a
// raw .266 or an MPEG-TS stream do not. Quick: call it before hours of analysis.
void check_writable(const VideoInfo& encoded);

// One VVC packet (an access unit, length-prefixed NAL units) as written: the
// packet as demuxed, its NAL length size, and the encoded frame it shows
// (from its presentation time). Called in decoding order.
using PacketEdit = std::function<vvc::Inserted(std::span<const std::uint8_t> packet, int length_size,
                                               std::int64_t index)>;

// The encode as an MKV with each VVC packet edited by `edit`. Throws
// std::invalid_argument (a bad output name), ToolError, Cancelled, or
// std::runtime_error (the check of the written file failed).
WriteReport write_vvc_mkv(const Tools& tools, const VideoInfo& encoded, const std::filesystem::path& output,
                          const PacketEdit& edit, const Progress& progress, CancelToken* cancel);

// The encode with an AFGS1 message on every picture. Throws as write_vvc_mkv.
WriteReport write_afgs1_mkv(const Tools& tools, const VideoInfo& encoded, const std::filesystem::path& output,
                            const GrainPlan& plan, int base_seed, const Progress& progress, CancelToken* cancel);

}  // namespace fgw
