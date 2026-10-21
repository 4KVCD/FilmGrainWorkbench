// VVC's own film grain: VVenC's analysis (--fga) and vvdec's synthesis,
// scene by scene.
//
// A scene (a short run of consecutive source frames) is cut from the source
// once and encoded by vvencapp in each film grain analysis mode, which writes
// the measured grain as an H.274 film grain characteristics (FGC) SEI
// message. vvdecapp decodes every encode twice: without the grain synthesis
// (the picture after VVenC removed the grain: its MCTF pre-filter plus coding
// at the chosen QP) and with it. The grain parameters each frame carries are
// read from the bitstream (ffmpeg drops the auto-regressive model).
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "fgw/picture.h"
#include "fgw/probe.h"
#include "fgw/rational.h"
#include "fgw/tools.h"

namespace fgw {

inline const std::vector<std::string> vvenc_presets = {"faster", "fast", "medium", "slow", "slower"};

// --fga modes and how the app names them.
struct FgaMode {
    int value;
    const char* label;
};
inline constexpr FgaMode fga_modes[] = {{1, "1 · VTM analysis (JVET-AN0237)"},
                                        {2, "2 · calibrated, temporal residual"},
                                        {3, "3 · auto-regressive grain model"}};

struct VvcTools {
    std::string vvencapp;
    std::string vvdecapp;
    std::vector<std::string> missing() const;
};

// vvencapp and vvdecapp from the app's bin folder, else the PATH.
VvcTools find_vvc_tools();

// The coding settings the three modes' encodes of a scene share.
struct VvcCoding {
    int strength = 100;  // --fgastrength, percent of the analysed grain
    double qp = 28.0;
    std::string preset = "faster";
    bool operator==(const VvcCoding&) const = default;
};

// What VVenC is asked to do for one encode.
struct VvcSettings {
    int fga = 2;  // --fga: 1 VTM analysis, 2 calibrated, 3 auto-regressive model
    VvcCoding coding;
    int frames = 33;  // the scene length: one 32-frame GOP plus the next intra frame
};

// One colour component of an FGC message: intervals of 8-bit intensity and
// their model values.
struct GrainPiece {
    int lower = 0;
    int upper = 0;
    std::vector<int> values;
    bool operator==(const GrainPiece&) const = default;
};

// The film grain parameters a frame carries (an H.274 FGC SEI message).
struct GrainSet {
    int model_id = 0;  // 0 frequency filtering, 1 auto-regressive
    int blending_mode_id = 0;  // 0 additive, 1 multiplicative
    int log2_scale_factor = 0;
    // Y, Cb, Cr; nothing for a component the message does not have.
    std::optional<std::vector<GrainPiece>> components[3];

    bool operator==(const GrainSet&) const = default;
    // The parameters as the side panel lists them.
    std::string describe() const;
};

// The film grain parameters in force for every picture of an Annex B
// bitstream, in display order (from each picture's order count, derived as
// vvdec does). A picture without a message keeps the previous parameters
// when their persistence flag says so, until a cancel message or the next
// coded video sequence. Throws std::invalid_argument for a stream it cannot read.
std::vector<std::optional<GrainSet>> read_grain(std::span<const std::uint8_t> bitstream);

// The source frames of a scene, cut once and encoded by every analysis mode.
struct SceneCut {
    std::int64_t start = 0;  // first source frame (a cut file may start later)
    std::int64_t count = 0;
    Geometry geometry;
    std::optional<Crop> crop;
    Rational fps;
    std::filesystem::path folder;
    std::string hdr = "off";  // vvencapp --hdr, from the source's colour tags

    std::filesystem::path source_file() const { return folder / "source.yuv"; }
};

// Which of a clip's pictures.
enum class ClipPicture { source, decoded, grained };

// A scene encoded and decoded with the given settings; frames live in raw files.
struct Clip {
    std::int64_t start = 0;  // first source frame
    std::int64_t count = 0;
    Geometry geometry;
    std::filesystem::path folder;  // this encode's own files
    VvcSettings settings;
    std::optional<Crop> crop;
    double fps = 0;
    std::filesystem::path source_file;  // the scene's source frames, shared with the other modes
    std::vector<std::optional<GrainSet>> grain;
    double encode_seconds = 0;

    bool covers(std::int64_t index) const { return start <= index && index < start + count; }
    std::filesystem::path bitstream() const { return folder / "clip.266"; }
    double kbps() const;
    // One of the clip's pictures of frame `index`. Throws std::out_of_range.
    Frame frame(ClipPicture which, std::int64_t index) const;
    const GrainSet* grain_at(std::int64_t index) const;
};

// The folder a tab keeps its scenes in, in the system's temporary folder,
// with a lock file kept open while it lives. Deleted, with everything in it,
// when it goes. Folders left by a crash are deleted when the next one is made;
// folders of windows still open are kept.
class WorkFolder {
public:
    explicit WorkFolder(const std::filesystem::path& parent = {});
    ~WorkFolder();
    WorkFolder(const WorkFolder&) = delete;
    WorkFolder& operator=(const WorkFolder&) = delete;

    const std::filesystem::path& path() const { return path_; }

private:
    std::filesystem::path path_;
    void* lock_ = nullptr;  // the open lock file (a Windows HANDLE)
};

// Letterbox and pillarbox borders, from ffmpeg's cropdetect at three places;
// the largest picture area found is kept, so a dark scene cannot crop real
// picture away. Nothing when there are no borders.
std::optional<Crop> detect_black_bars(const std::string& ffmpeg, const VideoInfo& info, CancelToken* cancel = nullptr);

// vvencapp --hdr for the source: an HDR encode adapts its QPs to PQ or HLG.
std::string vvenc_hdr_mode(const VideoInfo& info);

// Writes `frames` source frames from `start` as raw 10-bit 4:2:0 (what VVenC
// codes) into `folder`, the input of every encode of the scene.
SceneCut cut_scene(const std::string& ffmpeg, const VideoInfo& info, std::int64_t start, std::int64_t frames,
                   const std::optional<Crop>& crop, const std::filesystem::path& folder, CancelToken* cancel,
                   const Progress& progress);

// Encodes a cut scene with VVenC in one analysis mode and decodes it twice,
// in a folder of its own, deleted again if anything fails or the work is cancelled.
Clip encode_scene(const VvcTools& tools, const SceneCut& cut, const VvcSettings& settings, CancelToken* cancel,
                  const Progress& progress);

// How much grain VVenC took out of a frame and how much vvdec puts back, in
// 8-bit units: standard deviations of source − decoded and grained − decoded,
// and the mean of grained − decoded (Y, Cb, Cr), on every other sample.
struct FrameStats {
    double removed[3] = {};
    double added[3] = {};
    double shift[3] = {};
};
FrameStats frame_stats(const Frame& source, const Frame& decoded, const Frame& grained);

}  // namespace fgw
