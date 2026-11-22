#include "fgw/vvc_fga.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <deque>
#include <fstream>
#include <iterator>
#include <stdexcept>
#include <string>

#include "fgw/frames.h"
#include "fgw/fullscan.h"
#include "fgw/text.h"
#include "fgw/vvc.h"

namespace fgw {

namespace {

using Bytes = std::vector<std::uint8_t>;

// The marker before each parameter set of --fga 4: a user data unregistered
// SEI message with this UUID (393cc818-0a4b-4cef-8e2d-f1605b9577c0) and five
// data bytes: the mode, then the first frame (POC) the set applies to, a
// big-endian two's complement int (the lowest int for the first set). With
// --fgastacked, 128 + the mode marks a set of that frame alone, fitted to the
// grain the encode lost.
constexpr std::array<std::uint8_t, 16> segment_marker_uuid = {0x39, 0x3c, 0xc8, 0x18, 0x0a, 0x4b, 0x4c, 0xef,
                                                              0x8e, 0x2d, 0xf1, 0x60, 0x5b, 0x95, 0x77, 0xc0};

// VVenC keeps the latest 160 frames it wrote, five GOPs of 32 (FGC_SEI_HISTORY).
constexpr std::size_t written_history = 160;

constexpr std::uint8_t fitted_flag = 0x80;

struct Marker {
    int mode = 0;
    std::int64_t first_frame = 0;
    bool fitted = false;  // the set of first_frame alone
};

std::optional<Marker> segment_marker(const vvc::SeiMessage& message) {
    const std::vector<std::uint8_t>& data = message.payload;
    if (message.payload_type != vvc::sei_user_data_unregistered || data.size() != segment_marker_uuid.size() + 5 ||
        !std::equal(segment_marker_uuid.begin(), segment_marker_uuid.end(), data.begin())) {
        return std::nullopt;
    }
    const std::size_t at = segment_marker_uuid.size();
    const auto poc = static_cast<std::int32_t>((std::uint32_t{data[at + 1]} << 24) | (std::uint32_t{data[at + 2]} << 16) |
                                               (std::uint32_t{data[at + 3]} << 8) | data[at + 4]);
    return Marker{data[at] & ~fitted_flag, poc, (data[at] & fitted_flag) != 0};
}

// ffmpeg's standard error without the key=value lines of -progress.
std::string without_progress(const std::string& log) {
    std::string out;
    std::size_t start = 0;
    while (start < log.size()) {
        const std::size_t end = std::min(log.find('\n', start), log.size());
        const std::string_view line(log.data() + start, end - start);
        const bool progress_line = line.find('=') != std::string_view::npos && line.find(' ') == std::string_view::npos;
        if (!progress_line) {
            out.append(line).push_back('\n');
        }
        start = end + 1;
    }
    return out;
}

// What a crash's exit code (a Windows exception code) means; nothing for an ordinary exit code.
std::optional<std::string> crash_name(int exit_code) {
    const auto code = static_cast<std::uint32_t>(exit_code);
    if ((code & 0xF0000000u) != 0xC0000000u) {
        return std::nullopt;
    }
    const char* meaning = code == 0xC0000094u   ? "integer division by zero"
                          : code == 0xC0000005u ? "access violation"
                          : code == 0xC00000FDu ? "stack overflow"
                          : code == 0xC0000374u ? "heap corruption"
                          : code == 0xC0000409u ? "stack buffer overrun"
                          : code == 0xC0000017u ? "out of memory"
                                                : "exception";
    return format("0x%08X, %s", code, meaning);
}

bool is_random_access_point(int picture_type) {
    return picture_type == vvc::nal_idr_w_radl || picture_type == vvc::nal_idr_n_lp ||
           picture_type == vvc::nal_cra || picture_type == vvc::nal_gdr;
}

}  // namespace

// ---------------------------------------------------------------- the parameter sets

void FgaSegments::add(int mode, std::int64_t first_frame, std::vector<std::uint8_t> payload) {
    if (mode < 1 || mode > 3) {
        throw std::invalid_argument("film grain analysis mode " + std::to_string(mode) + " does not exist");
    }
    auto& sets = by_mode_[mode - 1];
    sets.erase(sets.lower_bound(first_frame), sets.end());
    sets[first_frame] = std::move(payload);
}

void FgaSegments::add_fitted(int mode, std::int64_t frame, std::vector<std::uint8_t> payload) {
    if (mode < 1 || mode > 3) {
        throw std::invalid_argument("film grain analysis mode " + std::to_string(mode) + " does not exist");
    }
    fitted_[mode - 1][frame] = std::move(payload);
}

bool FgaSegments::has(int mode) const { return mode >= 1 && mode <= 3 && !by_mode_[mode - 1].empty(); }

bool FgaSegments::has_fitted(int mode, std::int64_t frame) const {
    return mode >= 1 && mode <= 3 && fitted_[mode - 1].contains(frame);
}

const std::vector<std::uint8_t>& FgaSegments::payload_at(int mode, std::int64_t frame) const {
    if (!has(mode)) {
        throw std::out_of_range("no film grain parameters of --fga " + std::to_string(mode));
    }
    if (const auto fitted = fitted_[mode - 1].find(frame); fitted != fitted_[mode - 1].end()) {
        return fitted->second;
    }
    const auto& sets = by_mode_[mode - 1];
    const auto after = sets.upper_bound(frame);
    return after == sets.begin() ? after->second : std::prev(after)->second;
}

FgaSegments read_fga_segments(std::span<const std::uint8_t> stream) {
    FgaSegments segments;
    std::optional<Marker> pending;  // the marker whose set comes next
    for (const std::span<const std::uint8_t> nal : annexb_nals(stream)) {
        if (nal.size() < 2 || vvc::nal_type(nal) != vvc::nal_prefix_sei) {
            continue;
        }
        for (vvc::SeiMessage& message : vvc::parse_sei_messages(nal).value_or(std::vector<vvc::SeiMessage>{})) {
            if (const std::optional<Marker> marker = segment_marker(message)) {
                pending = marker;
            } else if (message.payload_type == vvc::sei_film_grain_characteristics && pending) {
                if (pending->fitted) {
                    segments.add_fitted(pending->mode, pending->first_frame, std::move(message.payload));
                } else {
                    segments.add(pending->mode, pending->first_frame, std::move(message.payload));
                }
                pending.reset();
            }
        }
    }
    for (const int mode : {1, 2, 3}) {
        if (!segments.has(mode) || (mode > 1 && !segments.has_fitted(mode, 0))) {
            throw std::invalid_argument("VVenC's film grain analysis wrote no parameters of --fga " +
                                        std::to_string(mode) + " (not the app's VVenC build?)");
        }
    }
    return segments;
}

// ---------------------------------------------------------------- the analysis

FgaAnalysis analyse_film_grain(const Tools& tools, const VvcTools& vvc_tools, const VideoInfo& source,
                               const VideoInfo& encoded, std::int64_t offset, const std::optional<Crop>& crop,
                               const VvcCoding& coding, const std::filesystem::path& work_folder,
                               const Progress& progress, CancelToken* cancel) {
    std::error_code error;
    if (vvc_tools.vvencapp.empty() || !std::filesystem::is_regular_file(utf8_path(vvc_tools.vvencapp), error)) {
        throw ToolError("Not found: vvencapp. " + missing_program_hint());
    }
    // A vvencapp built before --fga 4 or --fgastacked refuses them: ask it first, so that the message says
    // what to do. Its --fullhelp names the UUID of the --fga 4 markers (on one line, unlike the wrapped words
    // around it) and the option.
    const std::string help = run_tool({vvc_tools.vvencapp, "--fullhelp"}, cancel).standard_output;
    if (help.find("393cc818-0a4b-4cef-8e2d-f1605b9577c0") == std::string::npos ||
        help.find("fgastacked") == std::string::npos) {
        throw ToolError("This vvencapp cannot analyse without encoding (--fga 4): rebuild it with "
                        "vvc\\build_vvc_tools.ps1.");
    }
    const Geometry g = geometry_of(encoded);
    if (g.subsampling_x != 1 || g.subsampling_y != 1 || (g.bit_depth != 8 && g.bit_depth != 10)) {
        throw std::invalid_argument("VVenC analyses 8- and 10-bit 4:2:0 video; the encode is " + g.pix_fmt());
    }
    const std::optional<Rational> vvenc_fps = vvenc_frame_rate(encoded.fps);
    if (!vvenc_fps) {
        throw std::invalid_argument(format("VVenC does not take the encode's frame rate (%.3f fps)", encoded.fps.value()));
    }
    const auto [first, last] = frame_range(encoded, source, offset);
    const std::int64_t count = last - first;
    const Rational fps = encoded.fps;

    // Every frame: the source frame on top of the encode's frame that shows it (vstack), so that VVenC fits
    // the grain to what the encode lost. Each frame's timestamp becomes its frame number from `first`, so vstack
    // pairs frames by time, as the AV1 analysis does (a frame one decoder drops costs one repeated frame instead
    // of shifting every later pair), and a constant frame rate output repeats a frame a decoder dropped: frame N
    // of the output is encoded frame first + N and the source frame it shows. The encode's own film grain is
    // exported, not added.
    const auto number = [&fps](const VideoInfo& info, std::int64_t shift) {
        return format("settb=%lld/%lld,setpts='round((T-%.6f)*%lld/%lld)%+lld'", fps.den, fps.num, info.first_pts,
                      fps.num, fps.den, -shift);
    };
    const std::string graph = format("[0:%d]", source.stream_index) + stack_filter_chain(source, g, crop) + "," +
                              number(source, first + offset) + "[s];" + format("[1:%d]", encoded.stream_index) +
                              stack_filter_chain(encoded, g, std::nullopt) + "," + number(encoded, first) + "[e];" +
                              "[s][e]vstack=inputs=2:shortest=1[o]";
    std::vector<std::string> ffmpeg = {tools.ffmpeg, "-v", "error", "-nostdin", "-progress", "pipe:2", "-copyts"};
    for (std::string& arg : seek_args(source, first + offset)) {
        ffmpeg.push_back(std::move(arg));
    }
    ffmpeg.insert(ffmpeg.end(), {"-i", utf8(source.path), "-export_side_data", "film_grain"});
    for (std::string& arg : seek_args(encoded, first)) {
        ffmpeg.push_back(std::move(arg));
    }
    ffmpeg.insert(ffmpeg.end(), {"-i", utf8(encoded.path), "-filter_complex", graph, "-map", "[o]", "-frames:v",
                                 std::to_string(count), "-fps_mode", "cfr", "-f", "rawvideo", "-pix_fmt", g.pix_fmt(),
                                 "pipe:1"});
    const std::filesystem::path output = work_folder / "fga-segments.266";
    std::vector<std::string> vvencapp = {vvc_tools.vvencapp, "-i", "-", "-s",
                                         std::to_string(g.width) + "x" + std::to_string(g.height), "-c",
                                         g.bit_depth > 8 ? "yuv420_10" : "yuv420", "--internal-bitdepth",
                                         std::to_string(g.bit_depth), "--fps", vvenc_fps->to_string(), "-f",
                                         std::to_string(count), "--qp", format("%g", coding.qp), "--preset",
                                         coding.preset, "--fgastrength", vvenc_strength(coding.strength), "--fga", "4",
                                         "--fgastacked", "1"};
    if (const std::string hdr = vvenc_hdr_mode(source); hdr != "off") {
        vvencapp.insert(vvencapp.end(), {"--hdr", hdr});
    }
    vvencapp.insert(vvencapp.end(), {"-o", utf8(output)});

    // Hours on every core for a film, while the window is browsed: below-normal priority.
    platform::ProcessOptions decoder_options;
    decoder_options.background = true;
    std::atomic<std::int64_t> decoded = 0;  // frames ffmpeg has passed on, for a failure's message too
    decoder_options.on_error_line = [&progress, &decoded, count](std::string_view line) {
        std::int64_t done = 0;
        if (line.starts_with("frame=") &&
            std::from_chars(line.data() + 6, line.data() + line.size(), done).ec == std::errc()) {
            decoded = done;
            progress(static_cast<double>(done) / static_cast<double>(count),
                     "VVenC's film grain analysis: " + with_thousands(done) + " of " + with_thousands(count) +
                         " frames");
        }
    };
    progress(0, "VVenC's film grain analysis of " + with_thousands(count) + " frames…");
    platform::ChildProcess decoder = [&] {
        try {
            return platform::ChildProcess::start(ffmpeg, decoder_options);
        } catch (const platform::ProcessError& error) {
            throw ToolError("ffmpeg could not be started: " + std::string(error.what()));
        }
    }();
    const CancelToken::Tracking decoder_tracking(cancel, decoder);
    platform::ProcessOptions encoder_options;
    encoder_options.background = true;
    encoder_options.input_from = &decoder;
    platform::ChildProcess encoder = [&] {
        try {
            return platform::ChildProcess::start(vvencapp, encoder_options);
        } catch (const platform::ProcessError& error) {
            throw ToolError("vvencapp could not be started: " + std::string(error.what()));
        }
    }();
    const CancelToken::Tracking encoder_tracking(cancel, encoder);
    // vvencapp's log, read so that it never waits for room in the pipe; its
    // end says why it failed (vvencapp reports errors there). Its progress
    // lines ("stats:") say nothing about that.
    std::deque<std::string> log;
    while (std::optional<std::string> line = encoder.read_line()) {
        if (line->find("stats:") != std::string::npos) {
            continue;
        }
        log.push_back(std::move(*line));
        if (log.size() > 12) {
            log.pop_front();
        }
    }
    const int encoder_code = encoder.wait();
    const int decoder_code = decoder.wait();
    if (cancel != nullptr) {
        cancel->check();
    }
    if (encoder_code != 0) {
        std::string why = encoder.standard_error();
        for (const std::string& line : log) {
            why += line + "\n";
        }
        if (const std::optional<std::string> crash = crash_name(encoder_code)) {
            throw ToolError("vvencapp crashed (" + *crash + ") after " + with_thousands(decoded) + " of " +
                            with_thousands(count) + " frames.\n" + why);
        }
        throw tool_failure(vvc_tools.vvencapp, encoder_code, why);
    }
    if (decoder_code != 0) {
        throw tool_failure(tools.ffmpeg, decoder_code, without_progress(decoder.standard_error()));
    }
    std::ifstream stream(output, std::ios::binary);
    const Bytes data((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    return FgaAnalysis{first, count, read_fga_segments(data)};
}

// ---------------------------------------------------------------- placing the messages

bool FgcPlacement::needed(std::int64_t frame, int picture_type, bool changed, bool starts_sequence) {
    if (starts_sequence || picture_type == vvc::nal_idr_w_radl || picture_type == vvc::nal_idr_n_lp) {
        written_.clear();  // a new coded video sequence, where no earlier message persists
    }
    const bool rasl = picture_type == vvc::nal_rasl;
    const bool first_written = written_.lower_bound(frame) == written_.begin();
    const bool after_rasl = !rasl && rasl_pending_;
    const bool result = is_random_access_point(picture_type) || first_written || after_rasl || changed;
    written_.insert(frame);
    while (written_.size() > written_history) {
        written_.erase(written_.begin());
    }
    rasl_pending_ = rasl && (rasl_pending_ || result);
    return result;
}

WriteReport write_fgc_mkv(const Tools& tools, const VideoInfo& encoded, const std::filesystem::path& output,
                          const FgaAnalysis& analysis, int fga, const Progress& progress, CancelToken* cancel) {
    FgcPlacement placement;
    bool sequence_ended = false;  // the packet before ended a coded video sequence
    const PacketEdit edit = [&](std::span<const std::uint8_t> packet, int length_size, std::int64_t index) {
        const std::int64_t frame = index - analysis.first;
        const std::vector<std::uint8_t>& payload = analysis.segments.payload_at(fga, frame);
        const vvc::PacketKind kind = vvc::packet_kind(packet, length_size);
        // A frame with a set of its own (fitted) carries it, as VVenC writes one on every picture whose set it fits.
        const bool changed =
            analysis.segments.has_fitted(fga, frame) || analysis.segments.payload_at(fga, frame - 1) != payload;
        const bool needed =
            kind.picture_type && placement.needed(frame, *kind.picture_type, changed, std::exchange(sequence_ended, false));
        sequence_ended = sequence_ended || kind.ends_sequence;
        return vvc::insert_fgc(packet, length_size,
                               needed ? std::optional(std::span<const std::uint8_t>(payload)) : std::nullopt);
    };
    return write_vvc_mkv(tools, encoded, output, edit, progress, cancel);
}

}  // namespace fgw
