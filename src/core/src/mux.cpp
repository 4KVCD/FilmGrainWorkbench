#include "fgw/mux.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <memory>
#include <sstream>

#include <nlohmann/json.hpp>

#include "demux.h"
#include "fgw/afgs1.h"
#include "fgw/mkv_writer.h"
#include "fgw/temp_folder.h"
#include "fgw/text.h"
#include "json_members.h"
#include "fgw/vvc.h"

namespace fgw {

using nlohmann::json;

PictureFormat picture_format(const VideoInfo& info) {
    return {info.width, info.height, info.bit_depth, info.subsampling_x, info.subsampling_y, false};
}

namespace {

std::string counted(std::int64_t n, const std::string& what) {
    return std::to_string(n) + " " + what + (n == 1 ? "" : "s");
}

// What mkvmerge carries over from the encode besides its VVC track, e.g. "1 audio track".
std::vector<std::string> copied_from(const json& info, const json& main_id) {
    std::vector<std::pair<std::string, int>> kinds;  // in the order met, as the Python app's dict
    for (const json& track : array_member(info, "tracks")) {
        if (track.value("id", json()) == main_id) {
            continue;
        }
        const std::string kind = track.value("type", "other");
        auto known = std::find_if(kinds.begin(), kinds.end(), [&kind](const auto& k) { return k.first == kind; });
        if (known == kinds.end()) {
            kinds.emplace_back(kind, 1);
        } else {
            ++known->second;
        }
    }
    static const std::map<std::string, std::string> names = {
        {"audio", "audio track"}, {"subtitles", "subtitle track"}, {"video", "other video track"}};
    std::vector<std::string> out;
    for (const auto& [kind, n] : kinds) {
        const auto name = names.find(kind);
        out.push_back(counted(n, name != names.end() ? name->second : kind + " track"));
    }
    std::int64_t chapters = 0;
    if (info.contains("chapters") && info.at("chapters").is_array()) {
        for (const json& edition : info.at("chapters")) {
            chapters += edition.value("num_entries", 0);
        }
    }
    if (chapters) {
        out.push_back(counted(chapters, "chapter"));
    }
    if (info.contains("attachments") && info.at("attachments").is_array() && !info.at("attachments").empty()) {
        out.push_back(counted(static_cast<std::int64_t>(info.at("attachments").size()), "attachment"));
    }
    return out;
}

// mkvmerge -J video-track property -> the option that sets it on the new track.
const std::vector<std::pair<std::string, std::string>> track_options = {
    {"language", "--language"},
    {"track_name", "--track-name"},
    {"default_track", "--default-track-flag"},
    {"forced_track", "--forced-display-flag"},
    {"display_dimensions", "--display-dimensions"},
    {"color_matrix_coefficients", "--color-matrix-coefficients"},
    {"color_bits_per_channel", "--color-bits-per-channel"},
    {"chroma_subsample", "--chroma-subsample"},
    {"chroma_siting", "--chroma-siting"},
    {"color_range", "--color-range"},
    {"color_transfer_characteristics", "--color-transfer-characteristics"},
    {"color_primaries", "--color-primaries"},
    {"max_content_light", "--max-content-light"},
    {"max_frame_light", "--max-frame-light"},
    {"chromaticity_coordinates", "--chromaticity-coordinates"},
    {"white_color_coordinates", "--white-color-coordinates"},
    {"max_luminance", "--max-luminance"},
    {"min_luminance", "--min-luminance"},
};

// Track-header copies of HDR10 static metadata. When the VVC stream carries
// the same data in SEI messages, these are left out: ffmpeg 9.0.1 fails with
// "File exists" (-17) whenever it applies film grain to a stream whose MKV
// track header has mastering-display or content-light-level metadata.
const std::vector<std::string> mastering_keys = {"chromaticity_coordinates", "white_color_coordinates",
                                                 "max_luminance", "min_luminance"};
const std::vector<std::string> light_level_keys = {"max_content_light", "max_frame_light"};

// A property value as the Python app passed it to mkvmerge (str(value)).
std::string option_value(const json& value) {
    if (value.is_boolean()) {
        return value.get<bool>() ? "1" : "0";
    }
    if (value.is_string()) {
        return value.get<std::string>();
    }
    if (value.is_number_float()) {
        return python_float(value.get<double>());
    }
    return value.dump();
}

// Our VVC track first, then every other track, chapter, tag and attachment.
// ffmpeg cannot be used here: its Matroska demuxer does not rebuild decode
// timestamps for VVC B-pictures, so `ffmpeg -c copy` rewrites them and breaks
// the presentation order. mkvmerge works on presentation timestamps only,
// which is what MKV stores.
std::vector<std::string> merge_with_mkvmerge(const std::string& mkvmerge, const std::filesystem::path& video,
                                             const std::filesystem::path& original, const std::filesystem::path& output,
                                             CancelToken* cancel, const std::vector<std::string>& skip) {
    const json info = json::parse(run_tool({mkvmerge, "-J", utf8(original)}, cancel).standard_output);
    std::vector<const json*> videos;
    for (const json& track : array_member(info, "tracks")) {
        if (track.value("type", "") == "video") {
            videos.push_back(&track);
        }
    }
    const json* main = nullptr;
    for (const json* track : videos) {
        const std::string codec = track->value("codec", "") + object_member(*track, "properties").value("codec_id", "");
        if (codec.find("VVC") != std::string::npos) {
            main = track;
            break;
        }
    }
    if (main == nullptr && !videos.empty()) {
        main = videos.front();
    }
    if (main == nullptr) {
        throw std::runtime_error("mkvmerge found no video track in the encode");
    }
    std::vector<std::string> options;
    const json& properties = object_member(*main, "properties");
    for (const auto& [key, flag] : track_options) {
        const auto value = properties.find(key);
        if (value == properties.end() || value->is_null() || (value->is_string() && value->get<std::string>().empty()) ||
            std::find(skip.begin(), skip.end(), key) != skip.end()) {
            continue;
        }
        options.insert(options.end(), {flag, "0:" + option_value(*value)});
    }
    const json container = object_member(info, "container").value("properties", json::object());
    if (const std::string title = container.value("title", ""); !title.empty()) {
        options.insert(options.end(), {"--title", title});
    }
    std::string other_video;
    for (const json* track : videos) {
        if (track != main) {
            other_video += (other_video.empty() ? "" : ",") + track->at("id").dump();
        }
    }
    std::vector<std::string> command = {mkvmerge, "--quiet", "-o", utf8(output)};
    command.insert(command.end(), options.begin(), options.end());
    command.push_back(utf8(video));
    if (other_video.empty()) {
        command.push_back("-D");
    } else {
        command.insert(command.end(), {"-d", other_video});
    }
    command.push_back(utf8(original));
    try {
        run_tool(command, cancel);
    } catch (const ToolError& error) {
        // mkvmerge exits with 1 for warnings; the file is complete then.
        if (error.exit_code() != 1 || !std::filesystem::is_regular_file(output)) {
            throw;
        }
    }
    return copied_from(info, main->value("id", json()));
}

std::vector<double> packet_times(const std::string& ffprobe, const std::filesystem::path& path, int stream,
                                 int count, CancelToken* cancel) {
    const std::string out = run_tool({ffprobe, "-v", "error", "-select_streams", std::to_string(stream),
                                      "-read_intervals", "%+#" + std::to_string(count), "-show_entries",
                                      "packet=pts_time", "-of", "csv=p=0", utf8(path)},
                                     cancel)
                                .standard_output;
    std::vector<double> times;
    std::istringstream lines(out);
    for (std::string line; std::getline(lines, line);) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        line.erase(std::min(line.size(), line.find(',')));  // MPEG-TS packets add a side-data field: "2.358,"
        if (!line.empty()) {
            times.push_back(line == "N/A" ? std::nan("") : std::stod(line));
        }
    }
    return times;
}

std::int64_t count_packets(const std::string& ffprobe, const std::filesystem::path& path, int stream,
                           CancelToken* cancel) {
    const std::string out = run_tool({ffprobe, "-v", "error", "-select_streams", std::to_string(stream),
                                      "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
                                      utf8(path)},
                                     cancel)
                                .standard_output;
    return std::stoll(out.substr(0, out.find(',')));
}

// The packet's time in seconds: pts × time base, rounded once (Python's float(Fraction)).
double seconds_of(std::int64_t ticks, const Rational& time_base) {
    return static_cast<double>(ticks * time_base.num) / static_cast<double>(time_base.den);
}

}  // namespace

void check_writable(const VideoInfo& encoded) {
    const Demuxer demuxer(encoded.path, encoded.stream_index);
    vvc::length_size_from_vvcc(demuxer.extradata());
}

WriteReport write_afgs1_mkv(const Tools& tools, const VideoInfo& encoded, const std::filesystem::path& output,
                            const GrainPlan& plan, int base_seed, const Progress& progress, CancelToken* cancel) {
    std::string extension = utf8(output.extension());
    std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char c) { return std::tolower(c); });
    if (extension != ".mkv") {
        throw std::invalid_argument("the output must be a .mkv file");
    }
    std::error_code error;
    if (std::filesystem::exists(output, error) && std::filesystem::equivalent(output, encoded.path, error)) {
        throw std::invalid_argument("the output must not overwrite the encoded input");
    }
    const PictureFormat picture = picture_format(encoded);
    // By GrainPlan::key_at. Up to 4,096: a whole-film plan has ~150,000 keys,
    // and pictures come in decode order, so a key is met again only within a
    // GOP; one dropped is made again, the same.
    constexpr std::size_t kept_templates = 4096;
    std::map<std::int64_t, std::unique_ptr<T35Template>> templates;
    const auto template_for = [&](std::int64_t index) -> const T35Template& {
        const std::int64_t key = plan.key_at(index);
        if (templates.size() >= kept_templates && !templates.contains(key)) {
            templates.clear();
        }
        auto& slot = templates[key];
        if (!slot) {
            const std::optional<GrainParams> params = plan.params_at(index);
            if (!params) {
                throw std::runtime_error("no grain parameters for picture " + std::to_string(index));
            }
            slot = std::make_unique<T35Template>(*params, picture);
        }
        return *slot;
    };
    const double fps = encoded.fps.value();
    if (!output.parent_path().empty()) {
        std::filesystem::create_directories(output.parent_path());
    }

    const TempFolder folder("afgs1wb-mux-", output.parent_path().empty() ? std::filesystem::current_path()
                                                                         : output.parent_path());
    const std::filesystem::path video_path = folder.path() / "video.mkv";
    std::int64_t pictures = 0;
    std::int64_t packets = 0;
    std::set<int> sei_types;
    {
        Demuxer demuxer(encoded.path, encoded.stream_index);
        const int length_size = vvc::length_size_from_vvcc(demuxer.extradata());
        const Rational time_base = demuxer.time_base();
        MatroskaVideoWriter writer(video_path, demuxer.extradata(), encoded.width, encoded.height,
                                   static_cast<std::int64_t>(std::nearbyint(1e9 / fps)));
        const std::int64_t total = encoded.frame_count();
        while (const std::optional<DemuxedPacket> packet = demuxer.next()) {
            if (!packet->pts) {
                throw std::runtime_error("packet " + std::to_string(packets) + " of the VVC stream has no timestamp");
            }
            const double seconds = seconds_of(*packet->pts, time_base);
            const auto index = std::max<std::int64_t>(
                0, static_cast<std::int64_t>(std::nearbyint((seconds - encoded.first_pts) * fps)));
            if (packets < 64) {
                const std::set<int> types = vvc::prefix_sei_types(packet->data, length_size);
                sei_types.insert(types.begin(), types.end());
            }
            const std::vector<std::uint8_t> payload = template_for(index).for_seed(picture_seed(base_seed, index));
            const vvc::Inserted inserted = vvc::insert_t35(packet->data, length_size, payload);
            pictures += inserted.is_picture ? 1 : 0;
            const std::int64_t duration_ms =
                packet->duration ? static_cast<std::int64_t>(std::nearbyint(seconds_of(packet->duration, time_base) * 1000))
                                 : 0;
            writer.write(inserted.packet, static_cast<std::int64_t>(std::nearbyint(seconds * 1000)), packet->keyframe,
                         duration_ms);
            ++packets;
            if (packets % 500 == 0) {
                if (cancel != nullptr) {
                    cancel->check();
                }
                progress(std::min(0.9, 0.9 * static_cast<double>(packets) / static_cast<double>(total)),
                         "Adding grain metadata: " + with_thousands(packets) + " of ~" + with_thousands(total) +
                             " pictures");
            }
        }
        writer.close();
    }

    progress(0.9, "Merging audio, subtitles and chapters from the encode…");
    const std::filesystem::path merged = folder.path() / "merged.mkv";
    std::vector<std::string> skip;
    WriteReport report;
    if (sei_types.contains(vvc::sei_mastering_display)) {
        skip.insert(skip.end(), mastering_keys.begin(), mastering_keys.end());
    }
    if (sei_types.contains(vvc::sei_content_light_level)) {
        skip.insert(skip.end(), light_level_keys.begin(), light_level_keys.end());
    }
    if (!skip.empty()) {
        report.notes.push_back(
            "HDR10 mastering / light-level metadata is kept in the video stream's SEI and not repeated in the MKV "
            "track header (ffmpeg 9 cannot apply grain when it is there).");
    }
    report.copied = merge_with_mkvmerge(tools.mkvmerge, video_path, encoded.path, merged, cancel, skip);

    progress(0.95, "Checking the written file…");
    const std::int64_t written = count_packets(tools.ffprobe, merged, 0, cancel);
    if (written != packets) {
        throw std::runtime_error("verification failed: wrote " + std::to_string(packets) +
                                 " VVC packets but the output contains " + std::to_string(written));
    }
    const std::vector<double> before = packet_times(tools.ffprobe, encoded.path, encoded.stream_index, 64, cancel);
    const std::vector<double> after = packet_times(tools.ffprobe, merged, 0, 64, cancel);
    const auto milliseconds = [](const std::vector<double>& times) {
        std::vector<double> out;
        for (const double t : times) {
            out.push_back(std::nearbyint(t * 1000));
        }
        return out;
    };
    if (milliseconds(before) != milliseconds(after)) {
        throw std::runtime_error("verification failed: the output's video timestamps differ from the input");
    }
    std::filesystem::rename(merged, output);
    progress(1.0, "Done");
    report.output = output;
    report.pictures = pictures;
    report.packets = packets;
    return report;
}

}  // namespace fgw
