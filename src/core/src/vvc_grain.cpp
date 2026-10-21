#include "fgw/vvc_grain.h"

#include <windows.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <future>
#include <mutex>
#include <regex>

#include "fgw/bits.h"
#include "fgw/frames.h"
#include "fgw/temp_folder.h"
#include "fgw/text.h"
#include "fgw/vvc.h"

namespace fgw {

std::vector<std::string> VvcTools::missing() const {
    std::vector<std::string> out;
    for (const auto& [name, path] : {std::pair{"vvencapp", &vvencapp}, std::pair{"vvdecapp", &vvdecapp}}) {
        std::error_code error;
        if (path->empty() || !std::filesystem::is_regular_file(utf8_path(*path), error)) {
            out.push_back(name);
        }
    }
    return out;
}

VvcTools find_vvc_tools() {
    const auto find = [](const std::string& name) {
        std::filesystem::path where = bundled_program(name);
        if (where.empty()) {
            where = find_on_path(name);
        }
        return where.empty() ? std::string() : utf8(where);
    };
    return {find("vvencapp"), find("vvdecapp")};
}

// ---------------------------------------------------------------- reading the FGC SEI messages

std::string GrainSet::describe() const {
    std::string text = std::string(model_id == 0 ? "frequency filtering" : "auto-regressive") + " model, " +
                       (blending_mode_id == 0 ? "additive" : "multiplicative") + " blending, log2_scale_factor " +
                       std::to_string(log2_scale_factor);
    static const char* names[] = {"Y", "Cb", "Cr"};
    for (int c = 0; c < 3; ++c) {
        const auto& pieces = components[c];
        text += std::string("\n") + names[c] + ": ";
        if (!pieces || pieces->empty()) {
            text += "no grain";
            continue;
        }
        std::string parts;
        for (const GrainPiece& piece : *pieces) {
            const std::vector<int>& v = piece.values;
            std::string rest;
            if (model_id == 0) {
                // H.274: a missing horizontal cut-off is 8, a missing vertical one equals the horizontal.
                const int horizontal = v.size() > 1 ? v[1] : 8;
                const int vertical = v.size() > 2 ? v[2] : horizontal;
                rest = " cut-off H" + std::to_string(horizontal) + " V" + std::to_string(vertical);
            } else if (v.size() > 1) {
                rest = " AR";
                for (std::size_t i = 1; i < v.size(); ++i) {
                    rest += " " + std::to_string(v[i]);
                }
            }
            parts += (parts.empty() ? "" : "; ") + std::to_string(piece.lower) + "–" + std::to_string(piece.upper) +
                     ": " + std::to_string(v.empty() ? 0 : v[0]) + rest;
        }
        text += parts;
    }
    return text;
}

namespace {

constexpr int sei_film_grain = 19;
constexpr int nal_sps = 15;
constexpr int nal_idr_w_radl = 7;
constexpr int nal_idr_n_lp = 8;
constexpr int nal_cra = 9;
constexpr int nal_radl = 2;
constexpr int nal_rasl = 3;

using Bytes = std::vector<std::uint8_t>;

// The NAL units of an Annex B byte stream, without their start codes (and
// without the zero byte of a four-byte one).
std::vector<std::span<const std::uint8_t>> annexb_nals(std::span<const std::uint8_t> data) {
    std::vector<std::size_t> starts;
    for (std::size_t i = 0; i + 2 < data.size();) {
        if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1) {
            starts.push_back(i + 3);
            i += 3;
        } else {
            ++i;
        }
    }
    std::vector<std::span<const std::uint8_t>> nals;
    for (std::size_t k = 0; k < starts.size(); ++k) {
        std::size_t end = k + 1 < starts.size() ? starts[k + 1] - 3 : data.size();
        while (end > starts[k] && data[end - 1] == 0) {
            --end;
        }
        nals.push_back(data.subspan(starts[k], end - starts[k]));
    }
    return nals;
}

// sps_log2_max_pic_order_cnt_lsb_minus4 + 4, read as vvdec reads the SPS (H.266 7.3.2.4).
int poc_lsb_bits(std::span<const std::uint8_t> sps) {
    const Bytes rbsp = vvc::unescape_rbsp(sps.subspan(2));
    BitReader r(rbsp);
    r.skip(8);  // sps id, vps id
    const int max_sublayers_minus1 = static_cast<int>(r.get(3));
    r.skip(4);  // chroma format, CTU size
    if (r.get(1)) {  // sps_ptl_dpb_hrd_params_present_flag
        r.skip(7 + 1 + 8 + 1 + 1);  // profile, tier, level, frame only, multilayer
        if (r.get(1)) {  // gci_present_flag
            r.skip(71);  // the constraint flags and idcs
            r.skip(r.get(8));  // gci_num_reserved_bits, the reserved bits
        }
        r.align();
        int present = 0;
        for (int i = 0; i < max_sublayers_minus1; ++i) {
            present += static_cast<int>(r.get(1));
        }
        r.align();
        r.skip(static_cast<std::size_t>(8 * present));  // sublayer_level_idc
        r.skip(32 * static_cast<std::size_t>(r.get(8)));  // ptl_num_sub_profiles, general_sub_profile_idc
    }
    r.skip(1);  // sps_gdr_enabled_flag
    if (r.get(1)) {  // sps_ref_pic_resampling_enabled_flag
        r.skip(1);
    }
    r.ue();
    r.ue();  // the picture size
    if (r.get(1)) {  // conformance window
        for (int i = 0; i < 4; ++i) {
            r.ue();
        }
    }
    if (r.get(1)) {
        throw std::invalid_argument("subpictures are not supported");
    }
    r.ue();  // bit depth
    r.skip(2);  // entropy coding sync, entry point offsets
    return static_cast<int>(r.get(4)) + 4;
}

struct PictureHeader {
    int poc_lsb = 0;
    bool non_reference = false;
};

// (ph_pic_order_cnt_lsb, ph_non_ref_pic_flag) from a picture header (H.266
// 7.3.2.8); nothing for a slice whose picture header is in a NAL unit of its own.
std::optional<PictureHeader> picture_header(std::span<const std::uint8_t> rbsp, bool in_slice_header, int lsb_bits) {
    BitReader r(rbsp);
    if (in_slice_header && r.get(1) == 0) {  // sh_picture_header_in_slice_header_flag
        return std::nullopt;
    }
    const bool irap_or_gdr = r.get(1) != 0;
    PictureHeader header;
    header.non_reference = r.get(1) != 0;  // ph_non_ref_pic_flag
    if (irap_or_gdr) {
        r.skip(1);  // ph_gdr_pic_flag
    }
    if (r.get(1)) {  // ph_inter_slice_allowed_flag
        r.skip(1);
    }
    r.ue();  // ph_pic_parameter_set_id
    header.poc_lsb = static_cast<int>(r.get(lsb_bits));
    return header;
}

struct FgcMessage {
    std::optional<GrainSet> set;  // nothing: a cancel message
    bool persists = false;
};

FgcMessage parse_fgc(std::span<const std::uint8_t> payload) {
    BitReader r(payload);
    if (r.get(1)) {  // fg_characteristics_cancel_flag
        return {};
    }
    GrainSet set;
    set.model_id = static_cast<int>(r.get(2));
    if (r.get(1)) {  // a separate colour description
        r.skip(3 + 3 + 1 + 8 + 8 + 8);
    }
    set.blending_mode_id = static_cast<int>(r.get(2));
    set.log2_scale_factor = static_cast<int>(r.get(4));
    bool present[3];
    for (bool& p : present) {
        p = r.get(1) != 0;
    }
    for (int c = 0; c < 3; ++c) {
        if (!present[c]) {
            continue;
        }
        const int intervals = static_cast<int>(r.get(8)) + 1;
        const int values = static_cast<int>(r.get(3)) + 1;
        std::vector<GrainPiece> pieces;
        for (int i = 0; i < intervals; ++i) {
            GrainPiece piece;
            piece.lower = static_cast<int>(r.get(8));
            piece.upper = static_cast<int>(r.get(8));
            for (int k = 0; k < values; ++k) {
                piece.values.push_back(r.se());
            }
            pieces.push_back(std::move(piece));
        }
        set.components[c] = std::move(pieces);
    }
    return {std::move(set), r.get(1) != 0};
}

struct Picture {
    int sequence = 0;  // coded video sequence
    std::int64_t poc = 0;
    std::vector<FgcMessage> messages;
};

}  // namespace

std::vector<std::optional<GrainSet>> read_grain(std::span<const std::uint8_t> bitstream) {
    try {
        std::optional<int> lsb_bits;
        std::vector<Picture> pictures;
        std::optional<PictureHeader> pending_header;  // from a picture header NAL unit, for its slices
        std::vector<FgcMessage> pending;
        int sequence = -1;
        std::int64_t previous_lsb = 0;
        std::int64_t previous_msb = 0;
        for (const auto nal : annexb_nals(bitstream)) {
            if (nal.size() < 2) {
                continue;
            }
            const int kind = nal[1] >> 3;
            const int temporal_id = (nal[1] & 7) - 1;
            if (kind == nal_sps) {
                lsb_bits = poc_lsb_bits(nal);
            } else if (kind == vvc::nal_prefix_sei) {
                for (const vvc::SeiMessage& message : vvc::parse_sei_messages(nal).value_or(std::vector<vvc::SeiMessage>{})) {
                    if (message.payload_type == sei_film_grain) {
                        pending.push_back(parse_fgc(message.payload));
                    }
                }
            } else if (kind == vvc::nal_picture_header || kind <= 11) {
                if (!lsb_bits) {
                    throw std::invalid_argument("a picture comes before the SPS");
                }
                const Bytes rbsp = vvc::unescape_rbsp(nal.subspan(2));
                if (kind == vvc::nal_picture_header) {
                    pending_header = picture_header(rbsp, false, *lsb_bits);
                    continue;
                }
                std::optional<PictureHeader> header = picture_header(rbsp, true, *lsb_bits);
                if (!header) {  // its picture header came in a picture header NAL unit
                    if (!pending_header) {
                        continue;  // a further slice of the same picture
                    }
                    header = std::exchange(pending_header, std::nullopt);
                }
                const std::int64_t max_lsb = std::int64_t{1} << *lsb_bits;
                const std::int64_t lsb = header->poc_lsb;
                std::int64_t msb = previous_msb;
                if (kind == nal_idr_w_radl || kind == nal_idr_n_lp ||
                    (kind == nal_cra && sequence < 0)) {
                    ++sequence;
                    msb = 0;
                } else if (lsb < previous_lsb && previous_lsb - lsb >= max_lsb / 2) {
                    msb = previous_msb + max_lsb;
                } else if (lsb > previous_lsb && lsb - previous_lsb > max_lsb / 2) {
                    msb = previous_msb - max_lsb;
                }
                // prevTid0Pic, as vvdec's xUpdatePreviousTid0POC.
                if (temporal_id == 0 && kind != nal_radl && kind != nal_rasl && !header->non_reference) {
                    previous_lsb = lsb;
                    previous_msb = msb;
                }
                pictures.push_back({sequence, msb + lsb, std::move(pending)});
                pending.clear();
            }
        }
        std::stable_sort(pictures.begin(), pictures.end(), [](const Picture& a, const Picture& b) {
            return a.sequence != b.sequence ? a.sequence < b.sequence : a.poc < b.poc;
        });
        std::vector<std::optional<GrainSet>> result;
        std::optional<GrainSet> current;
        std::optional<int> current_sequence;
        for (Picture& picture : pictures) {
            if (picture.sequence != current_sequence) {
                current.reset();  // persistence ends with its sequence
                current_sequence = picture.sequence;
            }
            if (!picture.messages.empty()) {
                FgcMessage& last = picture.messages.back();
                result.push_back(last.set);
                current = last.persists ? last.set : std::nullopt;
            } else {
                result.push_back(current);
            }
        }
        return result;
    } catch (const std::out_of_range& error) {
        throw std::invalid_argument(std::string("the bitstream ends inside a header: ") + error.what());
    }
}

// ---------------------------------------------------------------- scenes and clips

double Clip::kbps() const {
    std::error_code error;
    const auto bytes = static_cast<double>(std::filesystem::file_size(bitstream(), error));
    return error ? 0.0 : bytes * 8 / 1000 / (static_cast<double>(count) / fps);
}

Frame Clip::frame(ClipPicture which, std::int64_t index) const {
    const std::int64_t k = index - start;
    if (k < 0 || k >= count) {
        throw std::out_of_range("frame " + std::to_string(index) + " is not in the clip " + std::to_string(start) + "–" +
                                std::to_string(start + count - 1));
    }
    const std::filesystem::path file = which == ClipPicture::source    ? source_file
                                       : which == ClipPicture::decoded ? folder / "decoded.yuv"
                                                                       : folder / "grain.yuv";
    return read_raw_frame(file, geometry, k, index);
}

const GrainSet* Clip::grain_at(std::int64_t index) const {
    const std::int64_t k = index - start;
    if (k < 0 || k >= static_cast<std::int64_t>(grain.size()) || !grain[static_cast<std::size_t>(k)]) {
        return nullptr;
    }
    return &*grain[static_cast<std::size_t>(k)];
}

namespace {

constexpr const char* work_prefix = "afgs1wb-vvc-";
constexpr const char* lock_name = ".in-use";

// Whether a running window still owns this work folder: its lock file is
// open, and Windows does not delete an open file. That answers even after a
// crash freed the owner's process id for reuse.
bool in_use(const std::filesystem::path& folder) {
    std::error_code error;
    std::filesystem::remove(folder / lock_name, error);
    return static_cast<bool>(error);
}

}  // namespace

WorkFolder::WorkFolder(const std::filesystem::path& parent_folder) {
    const std::filesystem::path parent =
        parent_folder.empty() ? std::filesystem::temp_directory_path() : parent_folder;
    std::error_code error;
    for (const auto& entry : std::filesystem::directory_iterator(parent, error)) {
        const std::string name = utf8(entry.path().filename());
        if (entry.is_directory(error) && name.starts_with(work_prefix) && !in_use(entry.path())) {
            std::filesystem::remove_all(entry.path(), error);
        }
    }
    path_ = make_unique_folder(work_prefix, parent);
    HANDLE lock = CreateFileW((path_ / lock_name).c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (lock != INVALID_HANDLE_VALUE) {
        const std::string pid = std::to_string(GetCurrentProcessId());
        DWORD written = 0;
        WriteFile(lock, pid.data(), static_cast<DWORD>(pid.size()), &written, nullptr);
        lock_ = lock;
    }
}

WorkFolder::~WorkFolder() {
    if (lock_ != nullptr) {
        CloseHandle(static_cast<HANDLE>(lock_));
    }
    std::error_code ignored;
    std::filesystem::remove_all(path_, ignored);
}

std::optional<Crop> detect_black_bars(const std::string& ffmpeg, const VideoInfo& info, CancelToken* cancel) {
    static const std::regex crop_pattern(R"(crop=(\d+):(\d+):(\d+):(\d+))");
    std::vector<Crop> boxes;
    for (const double fraction : {0.3, 0.5, 0.7}) {
        const auto index = static_cast<std::int64_t>(static_cast<double>(info.frame_count()) * fraction);
        std::vector<std::string> command = {ffmpeg, "-hide_banner", "-nostdin", "-copyts"};
        for (std::string& arg : seek_args(info, index)) {
            command.push_back(std::move(arg));
        }
        command.insert(command.end(), {"-i", utf8(info.path), "-map", "0:" + std::to_string(info.stream_index),
                                       "-frames:v", "24", "-an", "-sn", "-dn", "-vf",
                                       "cropdetect=limit=0.1:round=2:reset=0", "-f", "null", "-"});
        std::mutex mutex;
        std::optional<Crop> last;
        RunOptions options;
        options.on_error_line = [&](std::string_view line) {
            std::match_results<std::string_view::const_iterator> match;
            if (std::regex_search(line.begin(), line.end(), match, crop_pattern)) {
                const std::lock_guard lock(mutex);
                last = Crop{std::stoi(match[1]), std::stoi(match[2]), std::stoi(match[3]), std::stoi(match[4])};
            }
        };
        run_tool(command, cancel, options);
        const std::lock_guard lock(mutex);
        if (last) {
            boxes.push_back(*last);
        }
    }
    if (boxes.empty()) {
        return std::nullopt;
    }
    const Crop best = *std::max_element(boxes.begin(), boxes.end(), [](const Crop& a, const Crop& b) {
        return static_cast<std::int64_t>(a.width) * a.height < static_cast<std::int64_t>(b.width) * b.height;
    });
    if ((best.width == info.width && best.height == info.height) || best.width < 64 || best.height < 64) {
        return std::nullopt;
    }
    return best;
}

std::string vvenc_hdr_mode(const VideoInfo& info) {
    const bool wide = info.primaries.starts_with("bt2020");
    if (info.transfer == "smpte2084") {
        return wide ? "pq_2020" : "pq";
    }
    if (info.transfer == "arib-std-b67") {
        return wide ? "hlg_2020" : "hlg";
    }
    return "off";
}

SceneCut cut_scene(const std::string& ffmpeg, const VideoInfo& info, std::int64_t start, std::int64_t frames,
                   const std::optional<Crop>& crop, const std::filesystem::path& folder, CancelToken* cancel,
                   const Progress& progress) {
    const int width = crop ? crop->width : info.width;
    const int height = crop ? crop->height : info.height;
    if (width % 2 || height % 2) {
        throw std::invalid_argument("VVenC needs an even picture size, not " + std::to_string(width) + "×" +
                                    std::to_string(height));
    }
    SceneCut cut;
    cut.geometry = Geometry{width, height, 10, 1, 1};  // VVenC codes 10-bit 4:2:0
    cut.crop = crop;
    cut.fps = info.fps;
    cut.folder = folder;
    cut.hdr = vvenc_hdr_mode(info);
    std::filesystem::create_directories(folder);
    progress(-1, "Reading source frames " + with_thousands(start) + "–" + with_thousands(start + frames - 1) + "…");
    std::mutex mutex;
    std::optional<double> first_time;
    RunOptions options;
    options.on_error_line = [&](std::string_view line) {
        if (const std::optional<double> time = showinfo_time(line)) {
            const std::lock_guard lock(mutex);
            if (!first_time) {
                first_time = time;
            }
        }
    };
    run_tool(ffmpeg_command(ffmpeg, info, start, frames, cut.geometry, crop, utf8(cut.source_file()), true), cancel,
             options);
    std::error_code error;
    const std::uintmax_t size = std::filesystem::file_size(cut.source_file(), error);
    cut.count = error ? 0 : static_cast<std::int64_t>(size / cut.geometry.frame_bytes());
    if (cut.count == 0) {
        throw std::runtime_error(utf8(info.path.filename()) + ": frame " + std::to_string(start) +
                                 " could not be decoded");
    }
    const std::lock_guard lock(mutex);
    cut.start = first_time ? frame_index(info, *first_time) : start;  // a cut file may start later
    return cut;
}

Clip encode_scene(const VvcTools& tools, const SceneCut& cut, const VvcSettings& settings, CancelToken* cancel,
                  const Progress& progress) {
    if (const std::vector<std::string> missing = tools.missing(); !missing.empty()) {
        std::string names;
        for (const std::string& name : missing) {
            names += (names.empty() ? "" : ", ") + name;
        }
        throw ToolError("Not found: " + names + ". " + missing_program_hint());
    }
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    const std::filesystem::path folder = cut.folder / ("fga" + std::to_string(settings.fga) + "-" + std::to_string(stamp));
    std::filesystem::create_directory(folder);
    try {
        const Geometry& g = cut.geometry;
        progress(-1, format("Encoding %lld frames with VVenC --fga %d (QP %g, %s)…", static_cast<long long>(cut.count),
                            settings.fga, settings.coding.qp, settings.coding.preset.c_str()));
        const auto started = std::chrono::steady_clock::now();
        std::vector<std::string> encode = {tools.vvencapp, "-i", utf8(cut.source_file()), "-s",
                                           std::to_string(g.width) + "x" + std::to_string(g.height), "-c", "yuv420_10",
                                           "--fps", cut.fps.to_string(), "-f", std::to_string(cut.count), "--qp",
                                           format("%g", settings.coding.qp), "--preset", settings.coding.preset,
                                           "--fga", std::to_string(settings.fga), "--fgastrength",
                                           std::to_string(settings.coding.strength)};
        if (cut.hdr != "off") {
            encode.insert(encode.end(), {"--hdr", cut.hdr});
        }
        encode.insert(encode.end(), {"-o", utf8(folder / "clip.266")});
        // VVenC and vvdec use every core, often while the window is browsed.
        RunOptions background;
        background.background = true;
        run_tool(encode, cancel, background);
        const double encode_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();

        progress(-1, format("Decoding --fga %d with vvdec, without and with its grain synthesis…", settings.fga));
        const auto decode = [&](const char* file, const char* synthesis) {
            run_tool({tools.vvdecapp, "-b", utf8(folder / "clip.266"), "-o", utf8(folder / file), "-fg", synthesis},
                     cancel, background);
        };
        auto without = std::async(std::launch::async, decode, "decoded.yuv", "0");
        auto with = std::async(std::launch::async, decode, "grain.yuv", "1");
        std::ifstream stream(folder / "clip.266", std::ios::binary);
        const Bytes bitstream((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
        std::vector<std::optional<GrainSet>> grain = read_grain(bitstream);
        without.get();
        with.get();
        for (const char* file : {"decoded.yuv", "grain.yuv"}) {
            const auto size = static_cast<std::int64_t>(std::filesystem::file_size(folder / file));
            if (size != cut.count * static_cast<std::int64_t>(g.frame_bytes())) {
                throw std::runtime_error(format("vvdec wrote %lld of %lld frames",
                                                static_cast<long long>(size / static_cast<std::int64_t>(g.frame_bytes())),
                                                static_cast<long long>(cut.count)));
            }
        }
        Clip clip;
        clip.start = cut.start;
        clip.count = cut.count;
        clip.geometry = g;
        clip.folder = folder;
        clip.settings = settings;
        clip.crop = cut.crop;
        clip.fps = cut.fps.value();
        clip.source_file = cut.source_file();
        clip.grain = std::move(grain);
        clip.encode_seconds = encode_seconds;
        return clip;
    } catch (...) {
        std::error_code ignored;
        std::filesystem::remove_all(folder, ignored);
        throw;
    }
}

FrameStats frame_stats(const Frame& source, const Frame& decoded, const Frame& grained) {
    const double scale = 1 << (decoded.geometry.bit_depth - 8);
    // Mean and population standard deviation of (a - b) / scale on every other sample.
    const auto moments = [scale](const Plane& a, const Plane& b) {
        double sum = 0;
        double squares = 0;
        std::size_t n = 0;
        for (int y = 0; y < a.height; y += 2) {
            const std::uint16_t* ra = a.row(y);
            const std::uint16_t* rb = b.row(y);
            for (int x = 0; x < a.width; x += 2) {
                const double d = (static_cast<double>(ra[x]) - rb[x]) / scale;
                sum += d;
                squares += d * d;
                ++n;
            }
        }
        const double mean = n ? sum / static_cast<double>(n) : 0.0;
        return std::pair{mean, std::sqrt(std::max(0.0, n ? squares / static_cast<double>(n) - mean * mean : 0.0))};
    };
    FrameStats stats;
    const Plane* src[3] = {&source.y, &source.u, &source.v};
    const Plane* dec[3] = {&decoded.y, &decoded.u, &decoded.v};
    const Plane* grn[3] = {&grained.y, &grained.u, &grained.v};
    for (int c = 0; c < 3; ++c) {
        stats.removed[c] = moments(*src[c], *dec[c]).second;
        const auto [mean, deviation] = moments(*grn[c], *dec[c]);
        stats.added[c] = deviation;
        stats.shift[c] = mean;
    }
    return stats;
}

}  // namespace fgw
