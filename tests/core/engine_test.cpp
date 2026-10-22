// The engine's logic that runs no program, against the Python app's own
// results: ffprobe's answers read, frame rates, ffmpeg commands, saved
// analyses, the grain-over-time samples and the FGC SEI reader.
#include <gtest/gtest.h>

#include <nlohmann/json.hpp>

#include "fgw/frames.h"
#include "fgw/fullscan.h"
#include "fgw/probe.h"
#include "fgw/rational.h"
#include "fgw/text.h"
#include "fgw/timeline.h"
#include "fgw/tuning.h"
#include "fgw/vvc_grain.h"
#include "reference.h"

namespace fgw::test {
namespace {

// A program that fails reports its exit code: mkvmerge's 1 (warnings, a
// complete file) must not fail Write. It was lost, so Write always failed.
TEST(RunTool, AFailureCarriesTheExitCode) {
    try {
        run_tool({"cmd.exe", "/c", "exit 1"});
        FAIL() << "no error";
    } catch (const ToolError& error) {
        EXPECT_EQ(error.exit_code(), 1);
        EXPECT_NE(std::string(error.what()).find("failed (exit code 1)"), std::string::npos) << error.what();
    }
}

TEST(Rational, LimitsDenominatorsAsPython) {
    const json reference = load_reference("engine.json");
    ASSERT_FALSE(reference.at("fractions").empty());
    for (const json& c : reference.at("fractions")) {
        const Rational r = Rational{c.at("num"), c.at("den")}.limit_denominator(c.at("limit"));
        EXPECT_EQ(r, (Rational{c.at("result").at(0), c.at("result").at(1)})) << c.dump();
    }
}

TEST(Probe, ReadsFfprobesAnswersAndBuildsCommandsAsPython) {
    const json reference = load_reference("engine.json");
    ASSERT_FALSE(reference.at("probe").at("videos").empty());
    for (const json& c : reference.at("probe").at("videos")) {
        const auto first_pts = earliest_time(c.at("packets").get<std::string>());
        ASSERT_TRUE(first_pts.has_value());
        const VideoInfo info = parse_probe(utf8_path(c.at("path").get<std::string>()),
                                           json::parse(c.at("streams").get<std::string>()), *first_pts);
        const json& e = c.at("info");
        const std::string name = c.at("name");
        EXPECT_EQ(info.stream_index, e.at("stream_index").get<int>()) << name;
        EXPECT_EQ(info.codec, e.at("codec").get<std::string>()) << name;
        EXPECT_EQ(info.width, e.at("width").get<int>()) << name;
        EXPECT_EQ(info.height, e.at("height").get<int>()) << name;
        EXPECT_EQ(info.fps, (Rational{e.at("fps").at(0), e.at("fps").at(1)})) << name;
        EXPECT_EQ(info.pix_fmt, e.at("pix_fmt").get<std::string>()) << name;
        EXPECT_EQ(info.bit_depth, e.at("bit_depth").get<int>()) << name;
        EXPECT_EQ(info.subsampling_x, e.at("subsampling").at(0).get<int>()) << name;
        EXPECT_EQ(info.subsampling_y, e.at("subsampling").at(1).get<int>()) << name;
        EXPECT_EQ(info.duration, e.at("duration").get<double>()) << name;
        EXPECT_EQ(info.first_pts, e.at("first_pts").get<double>()) << name;
        EXPECT_EQ(info.color_range, e.at("color_range").get<std::string>()) << name;
        EXPECT_EQ(info.matrix, e.at("matrix").get<std::string>()) << name;
        EXPECT_EQ(info.transfer, e.at("transfer").get<std::string>()) << name;
        EXPECT_EQ(info.primaries, e.at("primaries").get<std::string>()) << name;
        EXPECT_EQ(info.start_time, e.at("start_time").get<double>()) << name;
        EXPECT_EQ(info.frames, e.at("frames").get<std::int64_t>()) << name;
        EXPECT_EQ(info.frame_count(), e.at("frame_count").get<std::int64_t>()) << name;
        EXPECT_EQ(info.describe(), c.at("describe").get<std::string>()) << name;

        const Geometry target{160, 92, 10, 1, 1};
        const Crop crop{300, 160, 10, 12};
        EXPECT_EQ(filter_chain(info, geometry_of(info), std::nullopt), c.at("filter_plain").get<std::string>());
        EXPECT_EQ(filter_chain(info, target, crop), c.at("filter_scaled").get<std::string>());
        EXPECT_EQ(seek_args(info, 17), c.at("seek_17").get<std::vector<std::string>>());
        EXPECT_EQ(ffmpeg_command("ffmpeg", info, 9, 4, target, crop, "pipe:1", true),
                  c.at("command").get<std::vector<std::string>>());
        EXPECT_EQ(ffmpeg_command("ffmpeg", info, 0, 1, geometry_of(info), std::nullopt, "x.yuv"),
                  c.at("command_plain").get<std::vector<std::string>>());
        EXPECT_EQ(frame_index(info, 1.5), c.at("frame_index_1_5").get<std::int64_t>());
    }
    // MPEG-TS packets carry side data: ffprobe's CSV lines end in a comma.
    // The Python app read none of them, so a VVC .ts did not open.
    EXPECT_EQ(earliest_time("2.400000,\n2.358000,\n2.441000,\n"), 2.358);
    for (const json& c : reference.at("probe").at("crops")) {
        VideoInfo source;
        source.width = c.at("source").at(0);
        source.height = c.at("source").at(1);
        VideoInfo encoded;
        encoded.width = c.at("encoded").at(0);
        encoded.height = c.at("encoded").at(1);
        const std::optional<Crop> crop = source_crop_for(source, encoded);
        if (c.at("crop").is_null()) {
            EXPECT_FALSE(crop.has_value()) << c.dump();
        } else {
            ASSERT_TRUE(crop.has_value()) << c.dump();
            EXPECT_EQ(*crop, (Crop{c.at("crop").at(0), c.at("crop").at(1), c.at("crop").at(2), c.at("crop").at(3)}));
        }
    }
}

TEST(Timeline, ReadsPythonsSavedAnalysesAndSamplesSigmasAsTheTuneLabel) {
    const json reference = load_reference("engine.json").at("timeline");
    TimelineModel model = TimelineModel::from_json(reference.at("json").get<std::string>());
    // Written again, the same analysis (the text may differ in its number formatting).
    EXPECT_EQ(json::parse(TimelineModel::from_json(model.to_json()).to_json()), json::parse(model.to_json()));
    EXPECT_EQ(model.alignment, (std::vector<std::string>{"ok", "unclear", "ok"}));
    EXPECT_EQ(model.warnings, std::vector<std::string>{"a warning"});
    EXPECT_TRUE(model.resumed);
    for (const json& c : reference.at("segment_at")) {
        EXPECT_EQ(model.segment_at(c.at("index")).start, c.at("start").get<std::int64_t>());
    }
    const std::vector<TableEntry> table = model.grain_table();
    ASSERT_EQ(table.size(), reference.at("grain_table").size());
    for (std::size_t i = 0; i < table.size(); ++i) {
        EXPECT_EQ(table[i].start, reference.at("grain_table").at(i).at("start").get<std::int64_t>());
        EXPECT_EQ(table[i].end, reference.at("grain_table").at(i).at("end").get<std::int64_t>());
    }
    model.sigma_frames.clear();
    model.sigma_values.clear();
    sample_sigmas(model, nullptr);
    EXPECT_EQ(model.sigma_frames, reference.at("sigma_frames").get<std::vector<std::int64_t>>());
    // The strip's sigma at a frame is the tune label's (the Python app's
    // strip used one seed of the label's three: 0.7 % apart here).
    ASSERT_EQ(model.sigma_values.size(), model.sigma_frames.size());
    for (std::size_t i = 0; i < model.sigma_frames.size(); ++i) {
        const GrainParams& params = model.segment_at(model.sigma_frames[i]).params;
        const double label = params.apply_grain && !params.y_points.empty()
                                 ? entry_sigmas(params, model.bit_depth, model.subsampling_x, model.subsampling_y).y[128]
                                 : 0.0;
        EXPECT_NEAR(model.sigma_values[i], label, 1e-9) << i;
    }
    EXPECT_THROW(TimelineModel::from_json(R"({"kind": "timeline", "version": 2})"), std::invalid_argument);
}

TEST(ReadGrain, ReadsSyntheticStreamsAsPython) {
    const json reference = load_reference("engine.json");
    ASSERT_FALSE(reference.at("read_grain").empty());
    for (const json& c : reference.at("read_grain")) {
        const std::vector<std::optional<GrainSet>> grain = read_grain(from_hex(c.at("stream")));
        ASSERT_EQ(grain.size(), c.at("describe").size()) << c.at("name");
        for (std::size_t i = 0; i < grain.size(); ++i) {
            const json& expected = c.at("describe").at(i);
            if (expected.is_null()) {
                EXPECT_FALSE(grain[i].has_value()) << c.at("name") << " picture " << i;
            } else {
                ASSERT_TRUE(grain[i].has_value()) << c.at("name") << " picture " << i;
                EXPECT_EQ(grain[i]->describe(), expected.get<std::string>());
            }
        }
    }
    EXPECT_TRUE(read_grain({}).empty());
}

}  // namespace
}  // namespace fgw::test
