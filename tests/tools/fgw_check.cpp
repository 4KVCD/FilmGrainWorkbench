// fgw_check: runs one engine function on real files and prints its result as
// JSON on standard output, for tools/compare_with_python.py, which runs the
// Python app's function on the same files and compares. A developer tool.
//
//   fgw_check probe VIDEO
//   fgw_check frames VIDEO START COUNT OUT.raw [W H DEPTH SUBX SUBY [CW CH CX CY]]
//   fgw_check blackbars VIDEO
//   fgw_check offset SOURCE ENCODED
//   fgw_check readgrain BITSTREAM.266
//   fgw_check estimate SOURCE ENCODED INDEX OFFSET
//   fgw_check analyze SOURCE ENCODED OFFSET WORK_DIR
//   fgw_check scene SOURCE START FRAMES FGA STRENGTH QP PRESET FOLDER
//   fgw_check write ENCODED OUTPUT.mkv TABLE.tbl SEED
#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>

#include <nlohmann/json.hpp>

#include "fgw/alignment.h"
#include "fgw/frames.h"
#include "fgw/fullscan.h"
#include "fgw/grain_params.h"
#include "fgw/mux.h"
#include "fgw/probe.h"
#include "fgw/text.h"
#include "fgw/tuning.h"
#include "fgw/vvc_grain.h"

using namespace fgw;
using nlohmann::json;

namespace {

json info_json(const VideoInfo& info) {
    return {{"stream_index", info.stream_index}, {"codec", info.codec}, {"width", info.width},
            {"height", info.height}, {"fps", {info.fps.num, info.fps.den}}, {"pix_fmt", info.pix_fmt},
            {"bit_depth", info.bit_depth}, {"subsampling", {info.subsampling_x, info.subsampling_y}},
            {"duration", info.duration}, {"first_pts", info.first_pts}, {"color_range", info.color_range},
            {"matrix", info.matrix}, {"transfer", info.transfer}, {"primaries", info.primaries},
            {"start_time", info.start_time}, {"frames", info.frames}, {"frame_count", info.frame_count()},
            {"describe", info.describe()}};
}

std::string read_file(const std::string& path) {
    std::ifstream file(utf8_path(path), std::ios::binary);
    std::ostringstream text;
    text << file.rdbuf();
    return text.str();
}

json run(const std::vector<std::string>& args, const Tools& tools) {
    const std::string& command = args.at(0);
    const auto video = [&](std::size_t i) { return probe(utf8_path(args.at(i)), tools.ffprobe); };
    const auto number = [&](std::size_t i) { return std::stoll(args.at(i)); };
    const Progress quiet = [](double, const std::string&) {};
    if (command == "probe") {
        return info_json(video(1));
    }
    if (command == "frames") {
        const VideoInfo info = video(1);
        std::optional<Geometry> target;
        std::optional<Crop> crop;
        if (args.size() >= 10) {
            target = Geometry{std::stoi(args[5]), std::stoi(args[6]), std::stoi(args[7]), std::stoi(args[8]),
                              std::stoi(args[9])};
        }
        if (args.size() >= 14) {
            crop = Crop{std::stoi(args[10]), std::stoi(args[11]), std::stoi(args[12]), std::stoi(args[13])};
        }
        const std::vector<Frame> frames = read_frames(tools.ffmpeg, info, number(2), number(3), nullptr, target, crop);
        std::ofstream out(utf8_path(args.at(4)), std::ios::binary);
        json indexes = json::array();
        for (const Frame& frame : frames) {
            indexes.push_back(frame.index);
            for (const Plane* plane : {&frame.y, &frame.u, &frame.v}) {
                out.write(reinterpret_cast<const char*>(plane->samples.data()), static_cast<std::streamsize>(plane->bytes()));
            }
        }
        return {{"indexes", indexes}};
    }
    if (command == "blackbars") {
        const std::optional<Crop> crop = detect_black_bars(tools.ffmpeg, video(1));
        return crop ? json{crop->width, crop->height, crop->x, crop->y} : json(nullptr);
    }
    if (command == "offset") {
        const VideoInfo source = video(1);
        const VideoInfo encoded = video(2);
        const DetectedOffset found = detect_offset(tools, source, encoded, source_crop_for(source, encoded));
        return {{"offset", found.offset}, {"confidence", found.confidence}};
    }
    if (command == "readgrain") {
        const std::string data = read_file(args.at(1));
        json out = json::array();
        for (const auto& set : read_grain({reinterpret_cast<const std::uint8_t*>(data.data()), data.size()})) {
            out.push_back(set ? json(set->describe()) : json(nullptr));
        }
        return out;
    }
    if (command == "estimate") {
        const VideoInfo source = video(1);
        const VideoInfo encoded = video(2);
        const std::int64_t index = number(3);
        const std::int64_t offset = number(4);
        const Frame encoded_frame = read_frame(tools.ffmpeg, encoded, index);
        const Frame source_frame = read_frame(tools.ffmpeg, source, index + offset, nullptr, geometry_of(encoded),
                                              source_crop_for(source, encoded));
        const GrainParams params = estimate_frame(source_frame, encoded_frame);
        return {{"table", format_grain_table({TableEntry{0, 1, params}})}};
    }
    if (command == "analyze") {
        const VideoInfo source = video(1);
        const VideoInfo encoded = video(2);
        const TimelineModel model = analyze_every_frame(tools, source, encoded, number(3),
                                                        source_crop_for(source, encoded), utf8_path(args.at(4)), quiet,
                                                        nullptr);
        std::vector<TableEntry> entries;
        for (const Segment& segment : model.segments()) {
            entries.push_back({segment.start, segment.end, segment.params});
        }
        return {{"table", format_grain_table(entries)}, {"alignment", model.alignment}, {"warnings", model.warnings},
                {"sigma_frames", model.sigma_frames}, {"sigma_values", model.sigma_values}};
    }
    if (command == "scene") {
        const VideoInfo source = video(1);
        const std::optional<Crop> crop = detect_black_bars(tools.ffmpeg, source);
        const SceneCut cut = cut_scene(tools.ffmpeg, source, number(2), number(3), crop, utf8_path(args.at(8)), nullptr,
                                       quiet);
        VvcSettings settings;
        settings.fga = std::stoi(args.at(4));
        settings.coding = {std::stoi(args.at(5)), std::stod(args.at(6)), args.at(7)};
        const Clip clip = encode_scene(find_vvc_tools(), cut, settings, nullptr, quiet);
        json grain = json::array();
        for (const auto& set : clip.grain) {
            grain.push_back(set ? json(set->describe()) : json(nullptr));
        }
        const Frame s = clip.frame(ClipPicture::source, clip.start + 1);
        const Frame d = clip.frame(ClipPicture::decoded, clip.start + 1);
        const Frame g = clip.frame(ClipPicture::grained, clip.start + 1);
        const FrameStats stats = frame_stats(s, d, g);
        return {{"start", clip.start}, {"count", clip.count}, {"hdr", cut.hdr},
                {"crop", crop ? json{crop->width, crop->height, crop->x, crop->y} : json(nullptr)},
                {"folder", utf8(clip.folder)}, {"grain", grain},
                {"stats", {{"removed", stats.removed}, {"added", stats.added}, {"shift", stats.shift}}}};
    }
    if (command == "write") {
        const VideoInfo encoded = video(1);
        const std::vector<TableEntry> entries = parse_grain_table(read_file(args.at(3)));
        const StaticPlan plan(entries.front().params);
        const WriteReport report = write_afgs1_mkv(tools, encoded, utf8_path(args.at(2)), plan,
                                                   static_cast<int>(number(4)), quiet, nullptr);
        return {{"pictures", report.pictures}, {"packets", report.packets}, {"notes", report.notes},
                {"copied", report.copied}};
    }
    throw std::invalid_argument("unknown command " + command);
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) {
        args.push_back(utf8(std::filesystem::path(argv[i])));
    }
    try {
        if (args.empty()) {
            throw std::invalid_argument("no command");
        }
        std::cout << run(args, detect_tools()).dump() << "\n";
        return 0;
    } catch (const std::exception& error) {
        std::cout << json{{"error", error.what()}}.dump() << "\n";
        return 1;
    }
}
