#include "fgw/fullscan.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <execution>
#include <fstream>
#include <future>
#include <map>
#include <numeric>
#include <sstream>

#include <nlohmann/json.hpp>

#include "fgw/alignment.h"
#include "fgw/frames.h"
#include "fgw/synth.h"
#include "fgw/temp_folder.h"
#include "fgw/text.h"

namespace fgw {

namespace {

constexpr int preflight_places = 8;   // alignment checks spread over the film before starting
constexpr int preflight_frames = 7;   // consecutive frames per check (detects offsets up to ±3)
constexpr std::int64_t plot_samples = 2000;  // points on the grain-over-time strip
constexpr double cpu_seconds_per_4k_frame = 1.85;  // analyser + decoding, one thread (The Beekeeper)
constexpr double thread_efficiency = 0.85;         // what each extra thread adds, measured on 24 threads
constexpr std::uint32_t checkpoint_magic = 0x31534741;  // "AGS1"

using Clock = std::chrono::steady_clock;

double seconds_since(Clock::time_point start) {
    return std::chrono::duration<double>(Clock::now() - start).count();
}

std::string read_text_file(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    std::ostringstream text;
    text << file.rdbuf();
    return text.str();
}

}  // namespace

std::filesystem::path analyzer_path() {
    const std::filesystem::path found = bundled_program("afgs1_grain_analyzer");
    return found.empty() ? app_dir() / "bin" / "afgs1_grain_analyzer.exe" : found;
}

int default_threads() { return cpu_count(); }

double estimate_seconds(const VideoInfo& encoded, int threads, const VideoInfo* source) {
    const double pixels = std::max(static_cast<double>(encoded.width) * encoded.height,
                                   source ? static_cast<double>(source->width) * source->height : 0.0);
    const double cpu_per_frame = cpu_seconds_per_4k_frame * pixels / (3840.0 * 2160.0);
    return static_cast<double>(encoded.frame_count()) * cpu_per_frame /
           std::max(1.0, std::min(threads, cpu_count()) * thread_efficiency);
}

std::pair<std::int64_t, std::int64_t> frame_range(const VideoInfo& encoded, const VideoInfo& source,
                                                  std::int64_t offset) {
    const std::int64_t first = std::max<std::int64_t>(0, -offset);
    const std::int64_t last = std::min(encoded.frame_count(), source.frame_count() - offset);
    if (last - first < 2) {
        throw std::invalid_argument("the source and encode do not overlap");
    }
    return {first, last};
}

std::string cache_key(const VideoInfo& source, const VideoInfo& encoded, std::int64_t offset,
                      const std::optional<Crop>& crop) {
    std::string key;
    for (const VideoInfo* info : {&source, &encoded}) {
        const FileStamp stamp = file_stamp(info->path);
        key += utf8(info->path) + "|" + std::to_string(stamp.size) + "|" + std::to_string(stamp.mtime) + "||";
    }
    // As the Python app wrote its Crop dataclass.
    key += std::to_string(offset) + "||" +
           (crop ? format("Crop(width=%d, height=%d, x=%d, y=%d)", crop->width, crop->height, crop->x, crop->y)
                 : std::string("None")) +
           "||analyzer-v2";
    return sha1_hex(key).substr(0, 16);
}

std::optional<std::int64_t> checkpoint_frame(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    std::uint32_t magic = 0;
    std::uint32_t version = 0;
    std::int64_t frame = 0;
    file.read(reinterpret_cast<char*>(&magic), sizeof magic);
    file.read(reinterpret_cast<char*>(&version), sizeof version);
    file.read(reinterpret_cast<char*>(&frame), sizeof frame);
    if (!file || magic != checkpoint_magic || version != 1) {
        return std::nullopt;
    }
    return frame;
}

namespace {

// Luma sigma at mid-grey (sample units) of one segment's parameters,
// averaged over the same seeds as the tune label's (entry_sigmas), so the
// strip and the label give the same number for a frame.
double segment_sigma(const GrainParams& params, int bit_depth) {
    if (params.y_points.empty() || !params.apply_grain) {
        return 0.0;
    }
    const std::array<int, 256> lut = scaling_lut(params.y_points);
    return static_cast<double>(lut[128]) / (1 << params.scaling_shift) * luma_template_std(params, bit_depth);
}

}  // namespace

void sample_sigmas(TimelineModel& model, CancelToken* cancel) {
    if (model.segments().empty() || model.frames <= 0) {
        return;
    }
    // numpy.unique(numpy.linspace(0, frames - 1, n).astype(int)).
    const std::int64_t n = std::min(plot_samples, model.frames);
    std::vector<std::int64_t> frames;
    const double step = n > 1 ? static_cast<double>(model.frames - 1) / static_cast<double>(n - 1) : 0.0;
    for (std::int64_t i = 0; i < n; ++i) {
        const double at = i == n - 1 ? static_cast<double>(model.frames - 1) : static_cast<double>(i) * step;
        frames.push_back(static_cast<std::int64_t>(at));
    }
    frames.erase(std::unique(frames.begin(), frames.end()), frames.end());

    // One sigma per segment met (~18 ms each), on all cores.
    std::vector<const Segment*> segments;
    std::map<std::int64_t, std::size_t> slot_of_start;
    std::vector<std::size_t> slots;
    for (const std::int64_t frame : frames) {
        const Segment& segment = model.segment_at(frame);
        const auto [where, added] = slot_of_start.emplace(segment.start, segments.size());
        if (added) {
            segments.push_back(&segment);
        }
        slots.push_back(where->second);
    }
    std::vector<double> sigmas(segments.size(), 0.0);
    std::vector<std::size_t> order(segments.size());
    std::iota(order.begin(), order.end(), std::size_t{0});
    std::for_each(std::execution::par, order.begin(), order.end(), [&](std::size_t i) {
        if (cancel == nullptr || !cancel->cancelled()) {
            sigmas[i] = segment_sigma(segments[i]->params, model.bit_depth);
        }
    });
    if (cancel != nullptr) {
        cancel->check();
    }
    model.sigma_frames = frames;
    model.sigma_values.clear();
    for (const std::size_t slot : slots) {
        model.sigma_values.push_back(sigmas[slot]);
    }
}

namespace {

// The first encoded frame at or after `first` that both files can decode. A
// file cut from a longer one starts with leading pictures that reference
// frames before the cut; ffmpeg drops them (a cut encode's first 15).
std::int64_t first_common_frame(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded,
                                std::int64_t offset, const std::optional<Crop>& crop, std::int64_t first,
                                CancelToken* cancel) {
    const std::int64_t encoded_index = read_frame(tools.ffmpeg, encoded, first, cancel).index;
    const std::int64_t source_index =
        read_frame(tools.ffmpeg, source, first + offset, cancel, geometry_of(encoded), crop).index - offset;
    if (std::min(encoded_index, source_index) < first) {
        throw std::runtime_error("seeking to frame " + std::to_string(first) + " returned frame " +
                                 std::to_string(std::min(encoded_index, source_index)));
    }
    return std::max(encoded_index, source_index);
}

// Every 4th sample of every 4th row of a luma plane, as floats.
LumaImage quarter_luma(const Plane& luma) {
    LumaImage image;
    image.width = (luma.width + 3) / 4;
    image.height = (luma.height + 3) / 4;
    image.samples.reserve(static_cast<std::size_t>(image.width) * image.height);
    for (int y = 0; y < luma.height; y += 4) {
        const std::uint16_t* row = luma.row(y);
        for (int x = 0; x < luma.width; x += 4) {
            image.samples.push_back(row[x]);
        }
    }
    return image;
}

// Whether the frames at encoded frame `place` line up, paired by their own timestamps.
Alignment check_place(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded, std::int64_t offset,
                      const std::optional<Crop>& crop, std::int64_t place, CancelToken* cancel) {
    const Geometry geometry = geometry_of(encoded);
    // Only a quarter-size luma of each frame is kept, not the frames (25 MB each at 4K).
    const auto quarter_lumas = [&](const VideoInfo& info, std::int64_t index, const std::optional<Geometry>& target,
                                   const std::optional<Crop>& frame_crop) {
        std::vector<LumaImage> lumas;
        std::vector<std::int64_t> indices = decode_frames(
            tools.ffmpeg, info, index, preflight_frames, [&lumas](Frame& frame) { lumas.push_back(quarter_luma(frame.y)); },
            cancel, target, frame_crop);
        return std::pair{std::move(indices), std::move(lumas)};
    };
    const auto [source_indices, source_lumas] = quarter_lumas(source, place + offset, geometry, crop);
    std::map<std::int64_t, const LumaImage*> source_by_index;
    for (std::size_t k = 0; k < source_lumas.size(); ++k) {
        source_by_index.emplace(source_indices[k] - offset, &source_lumas[k]);
    }
    const auto [encoded_indices, encoded_lumas] = quarter_lumas(encoded, place, std::nullopt, std::nullopt);
    std::vector<LumaImage> source_run;
    std::vector<LumaImage> encoded_run;
    for (std::size_t k = 0; k < encoded_lumas.size(); ++k) {
        if (const auto paired = source_by_index.find(encoded_indices[k]); paired != source_by_index.end()) {
            source_run.push_back(*paired->second);
            encoded_run.push_back(encoded_lumas[k]);
        }
    }
    if (source_run.size() < 3) {
        return Alignment::unclear;
    }
    return check_alignment(source_run, encoded_run, static_cast<double>((1 << geometry.bit_depth) - 1)).second;
}

// Checks frame-accurate pairing at several places before hours of work.
// Frames are paired by their own timestamps, like the every-frame pipeline,
// and the places stay clear of the ends, where cut files have frames that do
// not decode. Two places at a time.
std::vector<std::string> preflight_alignment(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded,
                                             std::int64_t offset, const std::optional<Crop>& crop,
                                             CancelToken* cancel, const Progress& progress, std::int64_t first) {
    const std::int64_t last = frame_range(encoded, source, offset).second;
    const std::int64_t margin = std::min<std::int64_t>(preflight_frames, std::max<std::int64_t>(0, (last - first) / 4));
    // numpy.linspace(first + margin, max(first, last - 7 - margin), 8).astype(int)
    const double from = static_cast<double>(first + margin);
    const double to = static_cast<double>(std::max(first, last - preflight_frames - margin));
    std::vector<std::int64_t> places;
    for (int i = 0; i < preflight_places; ++i) {
        const double at = i == preflight_places - 1 ? to : from + i * ((to - from) / (preflight_places - 1));
        places.push_back(static_cast<std::int64_t>(at));
    }
    std::vector<std::string> verdicts;
    for (std::size_t i = 0; i < places.size(); i += 2) {
        auto second = i + 1 < places.size()
                          ? std::async(std::launch::async, check_place, std::cref(tools), std::cref(source),
                                       std::cref(encoded), offset, std::cref(crop), places[i + 1], cancel)
                          : std::future<Alignment>();
        verdicts.push_back(to_string(check_place(tools, source, encoded, offset, crop, places[i], cancel)));
        progress(-1, format("Checking that source and encode frames line up (%zu of %zu)…", verdicts.size(),
                            places.size()));
        if (second.valid()) {
            verdicts.push_back(to_string(second.get()));
            progress(-1, format("Checking that source and encode frames line up (%zu of %zu)…", verdicts.size(),
                                places.size()));
        }
    }
    const auto count = [&verdicts](const char* verdict) {
        return static_cast<int>(std::count(verdicts.begin(), verdicts.end(), verdict));
    };
    const int shifted = count("shifted");
    if (shifted * 2 > static_cast<int>(verdicts.size()) || (shifted >= 2 && count("ok") == 0)) {
        throw std::runtime_error(format("Source and encode frames do not line up at %d of %zu places checked (they "
                                        "match neighbouring frames better). Use Detect to set the frame offset, then "
                                        "start again.",
                                        shifted, verdicts.size()));
    }
    return verdicts;
}

std::string chroma_name(const Geometry& geometry) {
    return geometry.subsampling_y ? "420" : geometry.subsampling_x ? "422" : "444";
}

// The decoder (both videos, stacked) and the analyzer's commands.
std::pair<std::vector<std::string>, std::vector<std::string>> pipeline_commands(
    const Tools& tools, const VideoInfo& source, const VideoInfo& encoded, std::int64_t offset,
    const std::optional<Crop>& crop, std::int64_t start, std::int64_t count, bool source_hwaccel,
    const std::filesystem::path& analyzer, int threads, const std::filesystem::path& table,
    const std::filesystem::path& checkpoint, bool resume) {
    const Geometry g = geometry_of(encoded);
    const Rational fps = encoded.fps;
    // Each frame's timestamp becomes its encoded-frame number, so vstack pairs
    // frames by time, not by arrival: a frame one decoder drops (or a file's
    // undecodable leading pictures) costs one repeated frame instead of
    // shifting every later pair. The analyzer numbers frames from `start`.
    const auto number = [&fps](const VideoInfo& info, std::int64_t shift) {
        return format("settb=%lld/%lld,setpts='round((T-%.6f)*%lld/%lld)%+lld'", fps.den, fps.num, info.first_pts,
                      fps.num, fps.den, -shift);
    };
    const std::string graph = format("[0:%d]", source.stream_index) + filter_chain(source, g, crop) + "," +
                              number(source, offset) + "[s];" + format("[1:%d]", encoded.stream_index) +
                              filter_chain(encoded, g, std::nullopt) + "," + number(encoded, 0) + "[e];" +
                              "[s][e]vstack=inputs=2:shortest=1[o]";
    std::vector<std::string> ffmpeg = {tools.ffmpeg, "-v", "error", "-nostdin"};
    if (source_hwaccel) {
        ffmpeg.insert(ffmpeg.end(), {"-hwaccel", "auto"});
    }
    ffmpeg.push_back("-copyts");
    for (std::string& arg : seek_args(source, start + offset)) {
        ffmpeg.push_back(std::move(arg));
    }
    ffmpeg.insert(ffmpeg.end(), {"-i", utf8(source.path)});
    for (std::string& arg : seek_args(encoded, start)) {
        ffmpeg.push_back(std::move(arg));
    }
    ffmpeg.insert(ffmpeg.end(), {"-i", utf8(encoded.path), "-filter_complex", graph, "-map", "[o]", "-frames:v",
                                 std::to_string(count), "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt",
                                 g.pix_fmt(), "pipe:1"});
    std::vector<std::string> analyzer_command = {
        utf8(analyzer),   "--width",         std::to_string(g.width),     "--height",
        std::to_string(g.height), "--bit-depth", std::to_string(g.bit_depth), "--chroma",
        chroma_name(g),   "--threads",       std::to_string(threads),     "--start-frame",
        std::to_string(start), "--output",   utf8(table),                 "--checkpoint",
        utf8(checkpoint), "--checkpoint-every", std::to_string(checkpoint_every)};
    if (resume) {
        analyzer_command.insert(analyzer_command.end(), {"--resume", utf8(checkpoint)});
    }
    return {ffmpeg, analyzer_command};
}

// The last `count` non-empty lines of a log, joined with " / ".
std::string last_lines(const std::string& log, std::size_t count) {
    std::vector<std::string> lines;
    std::istringstream stream(log);
    for (std::string line; std::getline(stream, line);) {
        if (!line.empty()) {
            lines.push_back(line);
        }
    }
    std::string out;
    for (std::size_t i = lines.size() > count ? lines.size() - count : 0; i < lines.size(); ++i) {
        out += (out.empty() ? "" : " / ") + lines[i];
    }
    return out;
}

// "Analysed 1,234 of 150,000 frames (11.2 fps, about 3 h 41 min left)"
std::string progress_text(std::int64_t done, std::int64_t first, std::int64_t last, double rate, bool resumed) {
    std::string text = "Analysed " + with_thousands(done - first) + " of " + with_thousands(last - first) + " frames";
    if (rate > 0) {
        const auto minutes_left = static_cast<std::int64_t>((static_cast<double>(last - done) / rate) / 60);
        const std::int64_t hours = minutes_left / 60;
        const std::int64_t minutes = minutes_left % 60;
        text += hours ? format(" (%.1f fps, about %lld h %02lld min left)", rate, hours, minutes)
                      : format(" (%.1f fps, about %lld min left)", rate, std::max<std::int64_t>(1, minutes));
    }
    return resumed ? text + " · resumed" : text;
}

struct RunResult {
    std::int64_t done = 0;  // the frame the analyzer reached
};

// Runs ffmpeg into the analyzer from frame `start` and reports progress.
RunResult run_pipeline(const std::vector<std::string>& ffmpeg_command, const std::vector<std::string>& analyzer_command,
                       std::int64_t first, std::int64_t start, std::int64_t last, bool resume,
                       const Progress& progress, CancelToken* cancel) {
    // Hours on every core, while the window is browsed: below-normal priority.
    platform::ProcessOptions decoder_options;
    decoder_options.background = true;
    platform::ChildProcess decoder = [&] {
        try {
            return platform::ChildProcess::start(ffmpeg_command, decoder_options);
        } catch (const platform::ProcessError& error) {
            throw ToolError("ffmpeg could not be started: " + std::string(error.what()));
        }
    }();
    const CancelToken::Tracking decoder_tracking(cancel, decoder);
    platform::ProcessOptions worker_options;
    worker_options.background = true;
    worker_options.input_from = &decoder;  // the analyzer owns the pipe now
    platform::ChildProcess worker = [&] {
        try {
            return platform::ChildProcess::start(analyzer_command, worker_options);
        } catch (const platform::ProcessError& error) {
            throw ToolError("the grain analyzer could not be started: " + std::string(error.what()));
        }
    }();
    const CancelToken::Tracking worker_tracking(cancel, worker);
    const Clock::time_point clock = Clock::now();
    RunResult result{start};
    while (const std::optional<std::string> line = worker.read_line()) {
        const bool progress_line = line->starts_with("P ");
        if (!progress_line && !line->starts_with("D ")) {
            continue;
        }
        result.done = std::stoll(line->substr(2));
        if (!progress_line) {
            continue;
        }
        const double elapsed = seconds_since(clock);
        const double rate = elapsed > 3 ? static_cast<double>(result.done - start) / elapsed : 0.0;
        progress(static_cast<double>(result.done - first) / static_cast<double>(last - first),
                 progress_text(result.done, first, last, rate, resume));
    }
    const int worker_code = worker.wait();
    const int decoder_code = decoder.wait();
    if (cancel != nullptr) {
        cancel->check();
    }
    if (worker_code != 0) {
        const std::string why = last_lines(worker.standard_error(), 3);
        throw ToolError("grain analyzer failed: " + (why.empty() ? "exit code " + std::to_string(worker_code) : why));
    }
    if (decoder_code != 0) {
        throw ToolError("ffmpeg failed while decoding: " + last_lines(decoder.standard_error(), 3));
    }
    return result;
}

}  // namespace

TimelineModel analyze_every_frame(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded,
                                  std::int64_t offset, const std::optional<Crop>& crop,
                                  const std::filesystem::path& work_dir, const Progress& progress,
                                  CancelToken* cancel, int threads, bool source_hwaccel,
                                  const std::filesystem::path& analyzer_override) {
    const Clock::time_point started = Clock::now();
    const std::filesystem::path analyzer = analyzer_override.empty() ? analyzer_path() : analyzer_override;
    if (!std::filesystem::is_regular_file(analyzer)) {
        throw ToolError("The grain analyzer is missing (" + utf8(analyzer) + "). " + missing_program_hint());
    }
    threads = threads > 0 ? threads : default_threads();
    auto [first, last] = frame_range(encoded, source, offset);
    std::filesystem::create_directories(work_dir);
    const std::filesystem::path table = work_dir / "table.tbl";
    const std::filesystem::path checkpoint = work_dir / "checkpoint.bin";
    const std::filesystem::path done_marker = work_dir / "done.json";

    std::vector<std::string> alignment;
    std::vector<std::string> warnings;
    double seconds = 0;  // how long the analysis itself took (not reading it back)
    bool resumed = false;
    if (std::filesystem::exists(done_marker) && std::filesystem::exists(table)) {
        const nlohmann::json marker = nlohmann::json::parse(read_text_file(done_marker), nullptr, false);
        if (marker.is_object()) {
            alignment = marker.value("alignment", std::vector<std::string>{});
            warnings = marker.value("warnings", std::vector<std::string>{});
            seconds = marker.value("seconds", 0.0);
            resumed = marker.value("resumed", false);
        }
    } else {
        const std::optional<std::int64_t> resume_at =
            std::filesystem::exists(table) ? checkpoint_frame(checkpoint) : std::nullopt;
        progress(-1, "Checking that source and encode frames line up…");
        first = first_common_frame(tools, source, encoded, offset, crop, first, cancel);
        alignment = preflight_alignment(tools, source, encoded, offset, crop, cancel, progress, first);
        const bool resume = resume_at && first < *resume_at && *resume_at < last;
        const std::int64_t start = resume ? *resume_at : first;
        const auto [ffmpeg_command, analyzer_command] =
            pipeline_commands(tools, source, encoded, offset, crop, start, last - start, source_hwaccel, analyzer,
                              threads, table, checkpoint, resume);
        const RunResult run = run_pipeline(ffmpeg_command, analyzer_command, first, start, last, resume, progress, cancel);
        if (last - run.done > std::max<std::int64_t>(2, static_cast<std::int64_t>(encoded.fps.value()))) {
            // The encode ends earlier than its header says: the last frames keep
            // the final segment's grain. Anything shorter is rounding of the length.
            warnings.push_back("Decoding stopped at frame " + with_thousands(run.done) + " of " +
                               with_thousands(last) + "; the frames after it use the last measured grain.");
        }
        if (first > 0) {
            warnings.push_back("Frames before " + with_thousands(first) +
                               " could not be decoded in both files; they use the first measured grain.");
        }
        seconds = seconds_since(started);  // this run only, if it resumed an earlier one
        resumed = resume;
        const nlohmann::json marker = {
            {"alignment", alignment}, {"warnings", warnings}, {"seconds", seconds}, {"resumed", resume}};
        std::ofstream(done_marker) << marker.dump();
        std::error_code ignored;
        std::filesystem::remove(checkpoint, ignored);
    }

    progress(0.99, "Summarising the grain over time…");
    std::vector<Segment> segments;
    for (TableEntry& entry : parse_grain_table(read_text_file(table))) {
        segments.push_back({entry.start, entry.end, std::move(entry.params)});
    }
    const Geometry geometry = geometry_of(encoded);
    TimelineModel model(std::move(segments), geometry.bit_depth, geometry.subsampling_x, geometry.subsampling_y,
                        encoded.fps, encoded.frame_count());
    model.seconds = seconds;
    model.alignment = alignment;
    model.warnings = warnings;
    model.resumed = resumed;
    sample_sigmas(model, cancel);
    return model;
}

GrainParams estimate_frame(const Frame& source, const Frame& encoded, CancelToken* cancel,
                           const std::filesystem::path& analyzer_override) {
    const Geometry& g = encoded.geometry;
    // The source above the encode, plane by plane, as ffmpeg's vstack lays them out.
    auto data = std::make_shared<std::string>();
    const std::size_t sample_bytes = static_cast<std::size_t>(g.sample_bytes());
    data->reserve(2 * g.frame_bytes());
    for (const auto& [above, below] : {std::pair{&source.y, &encoded.y}, std::pair{&source.u, &encoded.u},
                                       std::pair{&source.v, &encoded.v}}) {
        for (const Plane* plane : {above, below}) {
            if (sample_bytes == 2) {
                data->append(reinterpret_cast<const char*>(plane->samples.data()), plane->bytes());
            } else {
                for (const std::uint16_t sample : plane->samples) {
                    data->push_back(static_cast<char>(sample));
                }
            }
        }
    }
    const TempFolder folder("afgs1wb-frame-");
    const std::filesystem::path table = folder.path() / "frame.tbl";
    const std::filesystem::path analyzer = analyzer_override.empty() ? analyzer_path() : analyzer_override;
    RunOptions options;
    options.input_data = data;
    run_tool({utf8(analyzer), "--width", std::to_string(g.width), "--height", std::to_string(g.height),
              "--bit-depth", std::to_string(g.bit_depth), "--chroma", chroma_name(g), "--threads", "1", "--output",
              utf8(table)},
             cancel, options);
    const std::vector<TableEntry> entries = parse_grain_table(read_text_file(table));
    if (entries.empty()) {
        GrainParams none;
        none.apply_grain = false;
        return none;
    }
    return entries.front().params;
}

}  // namespace fgw
