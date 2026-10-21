// Grain parameters, AFGS1 coding, synthesis and tuning against the Python
// app's own results (tests/reference).
#include <gtest/gtest.h>

#include <cmath>
#include <filesystem>

#include "fgw/afgs1.h"
#include "fgw/grain_params.h"
#include "fgw/synth.h"
#include "fgw/tuning.h"
#include "reference.h"

namespace fgw::test {
namespace {

std::vector<GrainParams> param_cases() {
    // Held in a local: a range-for over a member of a temporary would see it
    // destroyed before the loop runs.
    const json reference = load_reference("params.json");
    std::vector<GrainParams> cases;
    for (const json& j : reference.at("cases")) {
        cases.push_back(params_from_json(j));
    }
    if (cases.empty()) {
        throw std::runtime_error("params.json has no cases");
    }
    return cases;
}

TEST(Synth, TemplatesLutsAndGrainedFramesMatchPython) {
    const std::vector<GrainParams> params = param_cases();
    const json reference = load_reference("synth.json");
    ASSERT_FALSE(reference.at("grain").empty());
    for (const json& c : reference.at("templates")) {
        const GrainTemplates t = generate_grain(params.at(c.at("params").get<std::size_t>()), params.at(c.at("params").get<std::size_t>()).random_seed,
                                                c.at("bit_depth"), c.at("subx"), c.at("suby"));
        EXPECT_EQ(sha256_ints(t.luma), c.at("luma").get<std::string>()) << c.dump();
        EXPECT_EQ(sha256_ints(t.cb), c.at("cb").get<std::string>()) << c.dump();
        EXPECT_EQ(sha256_ints(t.cr), c.at("cr").get<std::string>()) << c.dump();
    }
    for (const json& c : reference.at("luts")) {
        std::vector<ScalingPoint> points;
        for (const json& p : c.at("points")) {
            points.push_back({p.at(0), p.at(1)});
        }
        const std::array<int, 256> lut = scaling_lut(points);
        EXPECT_EQ(std::vector<int>(lut.begin(), lut.end()), c.at("lut").get<std::vector<int>>()) << c.dump();
    }
    for (const json& c : reference.at("grain")) {
        Frame frame = synthetic_frame(c.at("width"), c.at("height"), c.at("bit_depth"), c.at("subx"), c.at("suby"));
        add_grain(frame, params.at(c.at("params").get<std::size_t>()), c.at("seed"));
        EXPECT_EQ(sha256_planes({&frame.y, &frame.u, &frame.v}), c.at("result").get<std::string>()) << c.dump();
    }
    for (const json& c : reference.at("template_std")) {
        const GrainParams& p = params.at(c.at("params").get<std::size_t>());
        if (!p.y_points.empty()) {
            EXPECT_NEAR(luma_template_std(p, 10), c.at("luma").get<double>(), 1e-9);
        }
        const auto [cb, cr] = chroma_template_std(p, 10, 1, 1);
        EXPECT_NEAR(cb, c.at("chroma_420").at(0).get<double>(), 1e-9);
        EXPECT_NEAR(cr, c.at("chroma_420").at(1).get<double>(), 1e-9);
    }
}

TEST(Afgs1, PayloadsTemplatesDecodingAndSeedsMatchPython) {
    const std::vector<GrainParams> params = param_cases();
    const json reference = load_reference("afgs1.json");
    std::vector<PictureFormat> formats;
    for (const json& f : reference.at("formats")) {
        formats.push_back({f.at(0), f.at(1), f.at(2), f.at(3), f.at(4)});
    }
    ASSERT_FALSE(reference.at("encode").empty());
    for (const json& c : reference.at("encode")) {
        const GrainParams& p = params.at(c.at("params").get<std::size_t>());
        const PictureFormat& format = formats.at(c.at("format").get<std::size_t>());
        const std::vector<std::uint8_t> payload = encode_t35(p, format, c.at("seed"));
        EXPECT_EQ(to_hex(payload), c.at("payload").get<std::string>()) << c.dump();
        EXPECT_EQ(to_hex(T35Template(p, format).for_seed(777)), c.at("template_seed_777").get<std::string>());
        const std::vector<DecodedSet> decoded = decode_t35(payload);
        ASSERT_EQ(decoded.size(), c.at("decoded").size());
        for (std::size_t i = 0; i < decoded.size(); ++i) {
            const json& expected = c.at("decoded").at(i);
            EXPECT_EQ(decoded[i].params, params_from_json(expected.at("params")));
            EXPECT_EQ(decoded[i].seed, expected.at("seed").get<int>());
            const json& f = expected.at("format");
            EXPECT_EQ(decoded[i].format, (PictureFormat{f.at(0), f.at(1), f.at(2), f.at(3), f.at(4), f.at(5)}));
        }
    }
    for (const json& c : reference.at("seeds")) {
        for (std::size_t i = 0; i < c.at("indexes").size(); ++i) {
            EXPECT_EQ(picture_seed(c.at("base"), c.at("indexes").at(i)), c.at("values").at(i).get<int>());
        }
    }
}

TEST(GrainTable, FormatsAndParsesAsPython) {
    const json reference = load_reference("grain_table.json");
    const std::vector<TableEntry> parsed = parse_grain_table(reference.at("text").get<std::string>());
    ASSERT_EQ(parsed.size(), reference.at("parsed").size());
    for (std::size_t i = 0; i < parsed.size(); ++i) {
        const json& expected = reference.at("parsed").at(i);
        EXPECT_EQ(parsed[i].start, expected.at("start").get<std::int64_t>());
        EXPECT_EQ(parsed[i].end, expected.at("end").get<std::int64_t>());
        EXPECT_EQ(parsed[i].params, params_from_json(expected.at("params"))) << i;
    }
    EXPECT_EQ(format_grain_table(parsed), reference.at("text").get<std::string>());

    // Each chroma set is padded to libaom's count by its own length: with Cb
    // coefficients and none for Cr, Cr was written short (padded by Cb's).
    TableEntry lopsided{0, 1, parsed.front().params};
    lopsided.params.ar_coeff_lag = 1;  // 4 luma, 5 chroma coefficients
    lopsided.params.ar_coeffs_y = {1, 2, 3, 4};
    lopsided.params.ar_coeffs_cb = {1, 2, 3, 4, 5};
    lopsided.params.ar_coeffs_cr = {};
    const std::string text = format_grain_table({lopsided});
    EXPECT_NE(text.find("\tcCr 0 0 0 0 0\n"), std::string::npos) << text;

    EXPECT_THROW(parse_grain_table("filmgrn1\nE 0 1 1 7391 0\n"), std::invalid_argument);
    EXPECT_THROW(parse_grain_table("not a table"), std::invalid_argument);
}

TEST(Tuning, ScaledParametersAndSigmasMatchPython) {
    const std::vector<GrainParams> params = param_cases();
    const json reference = load_reference("tuning.json");
    ASSERT_FALSE(reference.at("scaled").empty());
    for (const json& c : reference.at("scaled")) {
        const Tuning tuning{c.at("luma"), c.at("chroma"), c.at("overlap"), c.at("clip"), c.at("seed")};
        const ScaledParams scaled = scale_params(params.at(c.at("params").get<std::size_t>()), tuning, c.at("subsampled_420"));
        EXPECT_EQ(scaled.params, params_from_json(c.at("result"))) << c.dump();
        EXPECT_EQ(scaled.clipped, c.at("clipped").get<bool>()) << c.dump();
    }
    for (const json& c : reference.at("sigmas")) {
        const Sigmas sigmas = entry_sigmas(params.at(c.at("params").get<std::size_t>()), 10, 1, 1);
        for (std::size_t i = 0; i < 256; ++i) {
            EXPECT_NEAR(sigmas.y[i], c.at("y").at(i).get<double>(), 1e-9);
            EXPECT_NEAR(sigmas.cb[i], c.at("cb").at(i).get<double>(), 1e-9);
            EXPECT_NEAR(sigmas.cr[i], c.at("cr").at(i).get<double>(), 1e-9);
        }
    }
}

TEST(Tuning, FramePlanScalesEstimatesAddedLater) {
    FramePlan plan({}, Tuning{2.0, 1.0}, true);
    EXPECT_FALSE(plan.params_at(5).has_value());
    GrainParams estimate;
    estimate.y_points = {{0, 100}, {255, 200}};
    plan.add_estimate(5, estimate);
    const std::optional<GrainParams> scaled = plan.params_at(5);
    ASSERT_TRUE(scaled.has_value());
    EXPECT_EQ(*scaled, scale_params(estimate, Tuning{2.0, 1.0}, true).params);
    EXPECT_TRUE(plan.clipped_at(5) == scale_params(estimate, Tuning{2.0, 1.0}, true).clipped);
}

}  // namespace
}  // namespace fgw::test
