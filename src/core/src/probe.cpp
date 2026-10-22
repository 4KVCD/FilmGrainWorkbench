#include "fgw/probe.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <optional>
#include <regex>
#include <sstream>

#include <nlohmann/json.hpp>

#include "fgw/text.h"
#include "json_members.h"

namespace fgw {

using nlohmann::json;

std::int64_t VideoInfo::frame_count() const {
    if (frames > 0) {
        return frames;
    }
    return std::max<std::int64_t>(1, static_cast<std::int64_t>(std::nearbyint(duration * fps.value())));
}

std::string VideoInfo::describe() const {
    // As Python's divmod(duration, 60) splits it.
    const double seconds = std::fmod(duration, 60.0);
    const double minutes_total = std::nearbyint((duration - seconds) / 60);
    const auto hours = static_cast<int>(minutes_total) / 60;
    const auto minutes = static_cast<int>(minutes_total) % 60;
    const std::string length =
        hours ? format("%d:%02d:%05.2f", hours, minutes, seconds) : format("%d:%05.2f", minutes, seconds);
    std::string upper = codec;
    std::transform(upper.begin(), upper.end(), upper.begin(), [](unsigned char c) { return std::toupper(c); });
    return format("%s %d×%d %.3f fps, %d-bit %s%s, ", upper.c_str(), width, height, fps.value(), bit_depth,
                  pix_fmt.c_str(), is_hdr() ? " HDR" : "") +
           length + " (" + with_thousands(frame_count()) + " frames)";
}

namespace {

// A JSON value as text: strings as they are, numbers as written.
std::string text_of(const json& object, const char* key) {
    const auto found = object.find(key);
    if (found == object.end() || found->is_null()) {
        return {};
    }
    return found->is_string() ? found->get<std::string>() : found->dump();
}

std::optional<double> to_double(const std::string& text) {
    if (text.empty()) {
        return std::nullopt;
    }
    double value = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc() || end != text.data() + text.size()) {
        return std::nullopt;
    }
    return value;
}

// Matroska's statistics DURATION tag ("01:29:04.955000000") in seconds; 0 if absent.
double tag_seconds(const std::string& value) {
    static const std::regex pattern(R"(\s*(\d+):(\d+):(\d+(?:\.\d+)?)\s*)");
    std::smatch match;
    if (!std::regex_match(value, match, pattern)) {
        return 0.0;
    }
    return std::stoi(match[1]) * 3600 + std::stoi(match[2]) * 60 + std::stod(match[3]);
}

// The exact picture count from the stream header or mkvmerge's statistics tags.
std::int64_t frame_count_of(const json& stream, const std::vector<std::pair<std::string, std::string>>& tags) {
    std::vector<std::string> values = {text_of(stream, "nb_frames")};
    for (const auto& [key, value] : tags) {
        if (key.starts_with("NUMBER_OF_FRAMES")) {
            values.push_back(value);
        }
    }
    for (const std::string& value : values) {
        std::int64_t count = 0;
        const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), count);
        if (error == std::errc() && end == value.data() + value.size() && count > 0) {
            return count;
        }
    }
    return 0;
}

std::string file_name(const std::filesystem::path& path) { return utf8(path.filename()); }

}  // namespace

std::optional<double> earliest_time(std::string_view csv) {
    std::optional<double> earliest;
    std::istringstream lines{std::string(csv)};
    for (std::string word; lines >> word;) {
        word.erase(std::min(word.size(), word.find(',')));  // MPEG-TS packets add a side-data field: "2.358,"
        if (const std::optional<double> value = to_double(word)) {
            earliest = earliest ? std::min(*earliest, *value) : *value;
        }
    }
    return earliest;
}

VideoInfo parse_probe(const std::filesystem::path& path, const json& data, double first_pts) {
    const json* stream = nullptr;
    for (const json& s : array_member(data, "streams")) {
        const bool attached_picture = s.contains("disposition") && s.at("disposition").value("attached_pic", 0) != 0;
        if (s.value("codec_type", "") == "video" && !attached_picture) {
            stream = &s;
            break;
        }
    }
    if (stream == nullptr) {
        throw ToolError(file_name(path) + " has no video stream");
    }
    const json& s = *stream;
    const std::string pix_fmt = text_of(s, "pix_fmt");
    static const std::regex pix_fmt_pattern(R"(^(?:yuv|yuvj)(420|422|444)p(?:(9|10|12|14|16)(le|be))?$)");
    std::smatch match;
    if (!std::regex_match(pix_fmt, match, pix_fmt_pattern)) {
        throw ToolError(file_name(path) + ": unsupported pixel format '" + pix_fmt +
                        "' (planar YUV 4:2:0, 4:2:2 or 4:4:4 required)");
    }
    const std::string chroma = match[1];
    const int depth = match[2].matched ? std::stoi(match[2]) : 8;
    if (depth > 12) {
        throw ToolError(file_name(path) + ": " + std::to_string(depth) + "-bit video is not supported (max 12-bit)");
    }

    const auto rate = [](const std::string& text) {
        const std::optional<Rational> parsed = Rational::parse(text.empty() ? "0/1" : text);
        return parsed && parsed->den != 0 ? parsed->reduced() : Rational{0, 1};
    };
    const std::string average = text_of(s, "avg_frame_rate");
    Rational fps = average == "0/0" ? Rational{0, 1} : rate(average);
    if (!fps.positive()) {
        fps = rate(text_of(s, "r_frame_rate"));
    }
    if (!fps.positive()) {
        throw ToolError(file_name(path) + ": frame rate unknown");
    }
    fps = fps.limit_denominator(1001000);

    std::vector<std::pair<std::string, std::string>> tags;
    for (const auto& [key, value] : object_member(s, "tags").items()) {
        std::string upper = key;
        std::transform(upper.begin(), upper.end(), upper.begin(), [](unsigned char c) { return std::toupper(c); });
        tags.emplace_back(upper, value.is_string() ? value.get<std::string>() : value.dump());
    }
    const json& format_section = object_member(data, "format");
    double duration = 0;
    if (const auto stream_duration = to_double(text_of(s, "duration"))) {
        duration = *stream_duration;
    } else {
        const auto tag = std::find_if(tags.begin(), tags.end(), [](const auto& t) { return t.first == "DURATION"; });
        duration = tag != tags.end() ? tag_seconds(tag->second) : 0.0;
        if (duration == 0) {
            duration = to_double(text_of(format_section, "duration")).value_or(0.0);
        }
    }
    if (duration <= 0) {
        throw ToolError(file_name(path) + ": duration unknown");
    }
    // MPEG-TS has no duration of its own: ffprobe estimates it from the
    // stream's start_time, its first packet's in decode order, which with
    // B-pictures is later than the first frame shown (a 2 s VVenC .ts read
    // as 1.04 s, so half its frames could not be reached).
    if (text_of(format_section, "format_name") == "mpegts") {
        if (const std::optional<double> start = to_double(text_of(s, "start_time")); start && *start > first_pts) {
            duration += *start - first_pts;
        }
    }

    VideoInfo info;
    info.path = path;
    info.stream_index = s.at("index").get<int>();
    info.codec = s.value("codec_name", "?");
    info.width = s.at("width").get<int>();
    info.height = s.at("height").get<int>();
    info.fps = fps;
    info.pix_fmt = pix_fmt;
    info.bit_depth = depth;
    info.subsampling_x = chroma != "444" ? 1 : 0;
    info.subsampling_y = chroma == "420" ? 1 : 0;
    info.duration = duration;
    info.first_pts = first_pts;
    info.color_range = text_of(s, "color_range");
    if (info.color_range.empty()) {
        info.color_range = "tv";
    }
    info.matrix = text_of(s, "color_space");
    info.transfer = text_of(s, "color_transfer");
    info.primaries = text_of(s, "color_primaries");
    info.start_time = to_double(text_of(format_section, "start_time")).value_or(0.0);
    info.frames = frame_count_of(s, tags);
    return info;
}

namespace {

// The smallest presentation time among the first packets. ffprobe's
// start_time is the first packet's in decode order, which for B-picture
// streams can be later than the first frame shown (ffmpeg's own libvvenc
// MKVs report 0.958 s for a stream that starts at 0); seeking relative to it
// lands on the wrong frame.
double first_presentation_time(const std::string& ffprobe, const std::filesystem::path& path, int stream,
                               CancelToken* cancel) {
    const std::string out = run_tool({ffprobe, "-v", "error", "-select_streams", std::to_string(stream),
                                      "-read_intervals", "%+#96", "-show_entries", "packet=pts_time", "-of",
                                      "csv=p=0", utf8(path)},
                                     cancel)
                                .standard_output;
    const std::optional<double> earliest = earliest_time(out);
    if (!earliest) {
        throw ToolError(file_name(path) + ": no packet timestamps found");
    }
    return *earliest;
}

}  // namespace

VideoInfo probe(const std::filesystem::path& path, const std::string& ffprobe, CancelToken* cancel) {
    std::error_code error;
    if (!std::filesystem::is_regular_file(path, error)) {
        throw ToolError("File not found: " + utf8(path));
    }
    const std::string out = run_tool({ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                      utf8(path)},
                                     cancel)
                                .standard_output;
    const json data = json::parse(out, nullptr, false);
    if (data.is_discarded()) {
        throw ToolError(file_name(path) + ": ffprobe's answer could not be read");
    }
    // The stream's index first, for the packet times; then everything with
    // them (an MPEG-TS duration depends on the first time).
    const int stream = parse_probe(path, data, 0.0).stream_index;
    return parse_probe(path, data, first_presentation_time(ffprobe, path, stream, cancel));
}

}  // namespace fgw
