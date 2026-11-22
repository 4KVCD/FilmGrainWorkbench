// fga-inject: VVenC's film grain analysis rerun on a VVC encode that exists
// already, its FGC SEI messages replaced by the new ones, written as MKV.
//
// For tuning the analysis without encoding: build vvencapp from the tree
// under test (vvc\build_fga_tool_vvencapp.ps1), then run this on an encode
// and its source. The analysis is the window's "Add the grain to your encode"
// (analyse_film_grain, write_fgc_mkv): vvencapp --fga 4 --fgastacked 1 reads
// each source picture above the encode's decoded picture and fits --fga 2's
// and 3's sets to the grain the encode lost, in the encoder's own picture
// order, layers and resets, which the encode's vvencapp options decide.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <exception>
#include <fstream>
#include <iostream>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "fgw/fullscan.h"
#include "fgw/mux.h"
#include "fgw/probe.h"
#include "fgw/temp_folder.h"
#include "fgw/text.h"
#include "fgw/tools.h"
#include "fgw/vvc_fga.h"
#include "fgw/vvc_grain.h"

namespace {

using namespace fgw;

constexpr const char* usage = R"(fga-inject: rerun VVenC's film grain analysis on an existing VVC encode and
replace its film grain (FGC SEI) messages, without encoding.

  fga-inject --encode E.mkv --source S [--source-frame N | --source-start SEC]
             --fga 2|3|2,3 --vvencapp X.exe --out O.mkv [--dump model.jsonl]
             -- <the vvencapp options the encode was made with>

  --encode        the VVC encode: MKV or MP4 (a raw .266 has no vvcC record to write from)
  --source        what it was encoded from: any file FFmpeg reads
  --source-frame  the source frame the encode's first frame shows (default 0)
  --source-start  the same in seconds: the first source frame at or after it
  --fga           the mode(s) written; one analysis serves both
  --vvencapp      a vvencapp with --fga 4 and --fgastacked (vvc\build_fga_tool_vvencapp.ps1)
  --out           the output MKV; with two modes, O_fga2.mkv and O_fga3.mkv
  --dump          one JSON line per mode and frame: the parameter set written
  after --         the encode's own options (QP, preset, refresh period, --hdr or --sdr,
                  threads...), without input, output, size, format, frame rate or
                  count and --fga: the tool sets those from the encode.

The source is decoded into a pipe at the encode's bit depth (4:2:0), converted
as an `ffmpeg -pix_fmt` pipe converts it (scaled with spline only when its size
differs from the encode's); ffmpeg, ffprobe and mkvmerge are found on the PATH.
Before the analysis the pairing of source and encode frames is checked at
eight places (a few seconds); a pairing that is off stops the run.
Everything runs at normal priority.
)";

struct Options {
    std::filesystem::path encode;
    std::filesystem::path source;
    std::optional<std::int64_t> source_frame;
    std::optional<double> source_start;
    std::vector<int> modes;
    std::string vvencapp;
    std::filesystem::path out;
    std::filesystem::path dump;
    std::vector<std::string> vvenc_args;
};

struct UsageError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

std::vector<int> parse_modes(const std::string& text) {
    if (text == "2") return {2};
    if (text == "3") return {3};
    if (text == "2,3" || text == "3,2") return {2, 3};
    throw UsageError("--fga takes 2, 3 or 2,3 (the fitted modes), not " + text);
}

Options parse(const std::vector<std::string>& args) {
    Options options;
    std::size_t i = 0;
    const auto value = [&](const std::string& name) -> const std::string& {
        if (++i >= args.size()) {
            throw UsageError(name + " needs a value");
        }
        return args[i];
    };
    // The option's value read whole by `read` (std::stoll or std::stod, as (text, &used)), or a usage error.
    const auto number = [&](const std::string& name, auto read) {
        const std::string& text = value(name);
        std::size_t used = 0;
        try {
            const auto result = read(text, &used);
            if (used == text.size()) {
                return result;
            }
        } catch (const std::logic_error&) {  // invalid_argument, out_of_range
        }
        throw UsageError(name + " takes a number, not " + text);
    };
    for (; i < args.size(); ++i) {
        const std::string& arg = args[i];
        if (arg == "--") {
            options.vvenc_args.assign(args.begin() + static_cast<std::ptrdiff_t>(i) + 1, args.end());
            break;
        }
        if (arg == "--encode") options.encode = utf8_path(value(arg));
        else if (arg == "--source") options.source = utf8_path(value(arg));
        else if (arg == "--source-frame") options.source_frame = number(arg, [](const std::string& s, std::size_t* used) { return std::stoll(s, used); });
        else if (arg == "--source-start") options.source_start = number(arg, [](const std::string& s, std::size_t* used) { return std::stod(s, used); });
        else if (arg == "--fga") options.modes = parse_modes(value(arg));
        else if (arg == "--vvencapp") options.vvencapp = value(arg);
        else if (arg == "--out") options.out = utf8_path(value(arg));
        else if (arg == "--dump") options.dump = utf8_path(value(arg));
        else if (arg == "-h" || arg == "--help") throw UsageError("");
        else throw UsageError("unknown option " + arg + " (the encode's vvencapp options go after --)");
    }
    if (options.encode.empty() || options.source.empty() || options.modes.empty() || options.vvencapp.empty() ||
        options.out.empty()) {
        throw UsageError("--encode, --source, --fga, --vvencapp and --out are needed");
    }
    if (options.source_frame && options.source_start) {
        throw UsageError("give --source-frame or --source-start, not both");
    }
    for (const std::string& arg : options.vvenc_args) {
        if (is_analysis_owned_vvenc_option(arg)) {
            throw UsageError("the tool sets " + arg + " itself from the encode: leave it out of the options after --");
        }
    }
    return options;
}

// The output of mode `fga`: --out itself, or with _fgaN before its extension when two modes are written.
std::filesystem::path output_of(const Options& options, int fga) {
    if (options.modes.size() == 1) {
        return options.out;
    }
    std::filesystem::path path = options.out;
    path.replace_filename(
        utf8_path(utf8(options.out.stem()) + "_fga" + std::to_string(fga) + utf8(options.out.extension())));
    return path;
}

// Progress on stderr: a line when the step changes and at every tenth of it.
Progress progress_printer() {
    return [last_tenth = -1, last_step = std::string()](double fraction, const std::string& message) mutable {
        const std::string step = message.substr(0, message.find(':'));
        const int tenth = static_cast<int>(std::floor(fraction * 10));
        if (step != last_step || tenth != last_tenth) {
            std::cerr << "  " << message << "\n";
            last_step = step;
            last_tenth = tenth;
        }
    };
}

nlohmann::json describe(const GrainSet& set) {
    nlohmann::json components = nlohmann::json::array();
    for (const auto& pieces : set.components) {
        nlohmann::json intervals = nlohmann::json::array();
        for (const GrainPiece& piece : pieces.value_or(std::vector<GrainPiece>{})) {
            intervals.push_back({{"lower", piece.lower}, {"upper", piece.upper}, {"values", piece.values}});
        }
        components.push_back(std::move(intervals));
    }
    return {{"model", set.model_id}, {"blending", set.blending_mode_id}, {"log2_scale", set.log2_scale_factor},
            {"components", std::move(components)}};
}

// One line per mode and frame: the set in force (the frame's fitted one, else its segment's) and its bytes.
// Model 0's values are the scale and the horizontal and vertical cut-offs; model 1's the scale and the AR terms.
void write_dump(const std::filesystem::path& path, const FgaAnalysis& analysis, const std::vector<int>& modes) {
    std::ofstream out(path, std::ios::binary);
    if (!out) {
        throw std::runtime_error("cannot write " + utf8(path));
    }
    for (const int mode : modes) {
        for (std::int64_t frame = 0; frame < analysis.count; ++frame) {
            const std::vector<std::uint8_t>& payload = analysis.segments.payload_at(mode, frame);
            std::string hex;
            for (const std::uint8_t byte : payload) {
                hex += format("%02x", byte);
            }
            const std::optional<GrainSet> set = parse_fgc_payload(payload);
            nlohmann::json line = {{"mode", mode},
                                   {"frame", frame},
                                   {"encode_frame", analysis.first + frame},
                                   {"fitted", analysis.segments.has_fitted(mode, frame)},
                                   {"set", set ? describe(*set) : nlohmann::json(nullptr)},
                                   {"payload", hex}};
            out << line.dump() << "\n";
        }
    }
}

double seconds_since(std::chrono::steady_clock::time_point start) {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
}

// Stops unless the encode's frame k shows source frame k + offset (preflight_alignment). The analysis fits the grain
// to the source minus the encode, picture by picture, and takes the scene cuts and the picture structure from the
// source: one frame off, it fits the grain to the difference of two pictures and places every cut a picture early
// or late. An encode whose source came through `ffmpeg -ss T ... -f yuv4mpegpipe` (or rawvideo) with T between two
// frames is such a case: the constant-frame-rate output writes the first frame twice, so every frame after the
// encode's first shows the source frame before the one --source-start T pairs it with (cut with -fps_mode
// passthrough instead).
void check_pairing(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded, std::int64_t offset) {
    const std::int64_t first = frame_range(encoded, source, offset).first;
    const auto wrong_offset = [offset](const std::string& why) {
        return std::runtime_error(why + "\nSource frame " + std::to_string(offset) +
                                  " is not what the encode's frames show: give the right --source-frame (and see "
                                  "that the encode does not start with a repeated frame).");
    };
    std::vector<std::string> verdicts;
    try {
        verdicts = preflight_alignment(tools, source, encoded, offset, std::nullopt, nullptr,
                                       [](double, const std::string&) {}, first);
    } catch (const ToolError&) {
        throw;
    } catch (const std::runtime_error& error) {
        throw wrong_offset(error.what());
    }
    std::string list;
    for (const std::string& verdict : verdicts) {
        list += " " + verdict;
    }
    std::cerr << "source and encode frames at " << verdicts.size() << " places:" << list << "\n";
    // The full scan tolerates a minority of shifted places; here one is enough: nothing but a wrong pairing lets
    // a run of seven frames match its neighbours better.
    if (std::count(verdicts.begin(), verdicts.end(), "shifted") > 0) {
        throw wrong_offset("Source and encode frames match neighbouring frames better at some of the places checked.");
    }
}

int run(const Options& options) {
    const Tools tools = detect_tools();
    if (const std::vector<std::string> missing = tools.missing(); !missing.empty()) {
        std::string names;
        for (const std::string& name : missing) {
            names += (names.empty() ? "" : ", ") + name;
        }
        throw ToolError("not found on the PATH: " + names);
    }
    const VideoInfo encoded = probe(options.encode, tools.ffprobe);
    check_writable(encoded);
    const VideoInfo source = probe(options.source, tools.ffprobe);
    std::int64_t offset = options.source_frame.value_or(0);
    if (options.source_start) {
        offset = static_cast<std::int64_t>(std::ceil(*options.source_start * source.fps.value() - 1e-3));
    }
    std::cerr << "ffmpeg " << tools.ffmpeg << "\nencode " << utf8(encoded.path) << ": " << encoded.width << "x"
              << encoded.height << " " << encoded.pix_fmt << " " << encoded.fps.to_string()
              << " fps\nsource frame " << offset << " is the encode's frame 0\n";
    check_pairing(tools, source, encoded, offset);

    const TempFolder work("fga-inject-");  // the analysis's stream, deleted at the end
    const VvcTools vvc_tools{options.vvencapp, {}};
    const auto start = std::chrono::steady_clock::now();
    const FgaAnalysis analysis = analyse_film_grain(tools, vvc_tools, source, encoded, offset, std::nullopt,
                                                    options.vvenc_args, work.path(), progress_printer(), nullptr,
                                                    /*background=*/false);
    std::cerr << format("analysed %lld frames (encode frames %lld to %lld) in %.1f s\n", analysis.count,
                        analysis.first, analysis.first + analysis.count - 1, seconds_since(start));
    if (!options.dump.empty()) {
        write_dump(options.dump, analysis, options.modes);
        std::cerr << "model dump: " << utf8(options.dump) << "\n";
    }
    for (const int fga : options.modes) {
        const auto write_start = std::chrono::steady_clock::now();
        const std::filesystem::path output = output_of(options, fga);
        const WriteReport report = write_fgc_mkv(tools, encoded, output, analysis, fga, progress_printer(), nullptr);
        std::cerr << format("--fga %d: %s, %lld pictures, in %.1f s\n", fga, utf8(output).c_str(), report.pictures,
                            seconds_since(write_start));
        for (const std::string& note : report.notes) {
            std::cerr << "  note: " << note << "\n";
        }
    }
    return 0;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) {
        args.push_back(utf8(std::filesystem::path(argv[i])));
    }
    try {
        return run(parse(args));
    } catch (const UsageError& error) {
        if (*error.what() != '\0') {
            std::cerr << "fga-inject: " << error.what() << "\n\n";
        }
        std::cerr << usage;
        return 2;
    } catch (const std::exception& error) {
        std::cerr << "fga-inject: " << error.what() << "\n";
        return 1;
    }
}
