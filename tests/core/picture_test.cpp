// The display conversion, VVC packet editing and the Matroska writer against
// the Python app's own results (tests/reference).
#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <iterator>

#include "fgw/display.h"
#include "fgw/mkv_writer.h"
#include "fgw/vvc.h"
#include "reference.h"

namespace fgw::test {
namespace {

// An RGB32 picture as R, G, B bytes (the layout the reference digests).
std::vector<std::uint8_t> rgb_bytes(const std::vector<std::uint32_t>& pixels) {
    std::vector<std::uint8_t> bytes;
    bytes.reserve(pixels.size() * 3);
    for (const std::uint32_t pixel : pixels) {
        bytes.push_back(static_cast<std::uint8_t>(pixel >> 16));
        bytes.push_back(static_cast<std::uint8_t>(pixel >> 8));
        bytes.push_back(static_cast<std::uint8_t>(pixel));
    }
    return bytes;
}

TEST(Display, RgbAndGrainOnlyPicturesMatchPython) {
    const json reference = load_reference("display.json");
    int compared = 0;
    for (const json& c : reference.at("cases")) {
        const int width = c.at("width");
        const int height = c.at("height");
        const int bit_depth = c.at("bit_depth");
        std::vector<std::uint32_t> pixels(static_cast<std::size_t>(width) * height);
        const RgbTarget target{pixels.data(), width};
        if (c.contains("grain_only")) {
            const Plane clean = synthetic_plane(width, height, bit_depth, 1);
            const Plane grained = synthetic_plane(width, height, bit_depth, 7);
            grain_only(clean, grained, bit_depth, target);
            std::vector<std::uint8_t> grey;
            for (const std::uint32_t pixel : pixels) {
                grey.push_back(static_cast<std::uint8_t>(pixel));
            }
            EXPECT_EQ(sha256(grey), c.at("grain_only").get<std::string>()) << c.dump();
            ++compared;
            continue;
        }
        const Frame frame = synthetic_frame(width, height, bit_depth, c.at("subx"), c.at("suby"));
        const Converter converter(DisplayFormat{bit_depth, c.at("limited"), c.at("matrix"), c.at("transfer"),
                                                c.at("primaries"), width, height});
        converter.to_rgb(frame, target);
        const std::vector<std::uint8_t> rgb = rgb_bytes(pixels);
        EXPECT_EQ(sha256(rgb), c.at("rgb").get<std::string>()) << c.dump();
        if (sha256(rgb) != c.at("rgb").get<std::string>()) {
            // Saved for a comparison with the Python app's picture, pixel by pixel.
            const auto path = std::filesystem::temp_directory_path() / ("fgw_display_" + std::to_string(compared) + ".rgb");
            std::ofstream(path, std::ios::binary).write(reinterpret_cast<const char*>(rgb.data()),
                                                        static_cast<std::streamsize>(rgb.size()));
            ADD_FAILURE() << "the picture is in " << path.string();
        }
        ++compared;
    }
    EXPECT_GT(compared, 0);
}

TEST(Display, LargeFramesConvertInBandsLikeInOnePiece) {
    const Frame frame = synthetic_frame(1500, 900, 10, 1, 1);  // above the band threshold
    const Converter converter(DisplayFormat{10, true, "bt2020nc", "smpte2084", "bt2020", 1500, 900});
    std::vector<std::uint32_t> banded(1500 * 900);
    converter.to_rgb(frame, {banded.data(), 1500});
    // The same rows converted one at a time, through a frame cut to each row.
    for (const int row : {0, 1, 449, 450, 898, 899}) {
        Frame one = frame;
        one.geometry.height = 2;
        one.y = Plane(1500, 2);
        std::copy_n(frame.y.row(row & ~1), 3000, one.y.samples.begin());
        one.u = Plane(750, 1);
        std::copy_n(frame.u.row(row / 2), 750, one.u.samples.begin());
        one.v = Plane(750, 1);
        std::copy_n(frame.v.row(row / 2), 750, one.v.samples.begin());
        std::vector<std::uint32_t> alone(3000);
        converter.to_rgb(one, {alone.data(), 1500});
        EXPECT_TRUE(std::equal(alone.begin() + (row & 1) * 1500, alone.begin() + (row & 1) * 1500 + 1500,
                               banded.begin() + row * 1500))
            << "row " << row;
    }
}

TEST(Vvc, PacketsSeiAndEscapingMatchPython) {
    const json reference = load_reference("vvc.json");
    const std::vector<std::uint8_t> payload = from_hex(reference.at("payload"));
    ASSERT_FALSE(reference.at("packets").empty());
    for (const json& c : reference.at("packets")) {
        const std::vector<std::uint8_t> packet = from_hex(c.at("packet"));
        const int length_size = c.at("length_size");
        const vvc::Inserted inserted = vvc::insert_t35(packet, length_size, payload);
        EXPECT_EQ(to_hex(inserted.packet), c.at("inserted").get<std::string>()) << c.dump();
        EXPECT_EQ(inserted.is_picture, c.at("is_picture").get<bool>());
        const std::set<int> types = vvc::prefix_sei_types(packet, length_size);
        EXPECT_EQ(std::vector<int>(types.begin(), types.end()), c.at("sei_types").get<std::vector<int>>());
        std::vector<std::string> t35;
        for (const auto& found : vvc::find_t35(inserted.packet, length_size)) {
            t35.push_back(to_hex(found));
        }
        EXPECT_EQ(t35, c.at("t35").get<std::vector<std::string>>());
    }
    for (const json& c : reference.at("escape")) {
        const std::vector<std::uint8_t> rbsp = from_hex(c.at("rbsp"));
        const std::vector<std::uint8_t> escaped = vvc::escape_rbsp(rbsp);
        EXPECT_EQ(to_hex(escaped), c.at("escaped").get<std::string>());
        EXPECT_EQ(to_hex(vvc::unescape_rbsp(escaped)), c.at("unescaped").get<std::string>());
    }
}

TEST(MatroskaWriter, WritesTheSameBytesAsPython) {
    const json reference = load_reference("mkv.json");
    const auto path = std::filesystem::temp_directory_path() / "fgw_mkv_writer_test.mkv";
    {
        const std::vector<std::uint8_t> codec_private = {0xFF, 0xF8, 1, 2, 3};
        MatroskaVideoWriter writer(path, codec_private, 1920, 1080, 41708333);
        for (int k = 0; k < 120; ++k) {
            std::vector<std::uint8_t> data(static_cast<std::size_t>(50 + k));
            for (std::size_t j = 0; j < data.size(); ++j) {
                data[j] = static_cast<std::uint8_t>((k * 7 + static_cast<int>(j)) % 256);
            }
            writer.write(data, k * 42 + (k > 60 ? 31000 : 0), k % 24 == 0, 42);
        }
        writer.close();
    }
    std::ifstream file(path, std::ios::binary);
    const std::vector<std::uint8_t> bytes((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();
    std::filesystem::remove(path);
    EXPECT_EQ(bytes.size(), reference.at("size").get<std::size_t>());
    EXPECT_EQ(sha256(bytes), reference.at("sha256").get<std::string>());
}

TEST(MatroskaWriter, AnUnfinishedFileIsRemoved) {
    const auto path = std::filesystem::temp_directory_path() / "fgw_mkv_writer_abort.mkv";
    {
        const std::vector<std::uint8_t> codec_private = {0xFF};
        MatroskaVideoWriter writer(path, codec_private, 64, 48);
        writer.write(codec_private, 0, true);
    }  // destroyed without close(): a cancelled write
    EXPECT_FALSE(std::filesystem::exists(path));
}

}  // namespace
}  // namespace fgw::test
