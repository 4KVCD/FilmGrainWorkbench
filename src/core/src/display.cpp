#include "fgw/display.h"

#include <algorithm>
#include <cmath>
#include <execution>
#include <map>

namespace fgw {

namespace {

constexpr int lut_size = 4096;
constexpr double sdr_white_nits = 203.0;
constexpr int band_count = 8;                      // threads a large frame is converted on
constexpr std::size_t band_min_samples = 1 << 20;  // smaller frames convert in one piece

// (Kr, Kb) of the YUV matrices.
const std::map<std::string, std::pair<double, double>> matrices = {
    {"bt709", {0.2126, 0.0722}},     {"bt2020nc", {0.2627, 0.0593}}, {"bt2020c", {0.2627, 0.0593}},
    {"bt470bg", {0.299, 0.114}},     {"smpte170m", {0.299, 0.114}},
};

// BT.2020 primaries to BT.709, linear light (rows: R, G, B out).
constexpr float bt2020_to_709[3][3] = {
    {1.6605f, -0.5876f, -0.0728f}, {-0.1246f, 1.1329f, -0.0083f}, {-0.0182f, -0.1006f, 1.1187f}};

double pq_to_nits(double e) {
    const double m1 = 2610.0 / 16384;
    const double m2 = 2523.0 / 4096 * 128;
    const double c1 = 3424.0 / 4096;
    const double c2 = 2413.0 / 4096 * 32;
    const double c3 = 2392.0 / 4096 * 32;
    const double p = std::pow(std::clamp(e, 0.0, 1.0), 1 / m2);
    return 10000 * std::pow(std::max(p - c1, 0.0) / (c2 - c3 * p), 1 / m1);
}

double hlg_to_nits(double e) {
    const double a = 0.17883277;
    const double b = 0.28466892;
    const double c = 0.55991073;
    e = std::clamp(e, 0.0, 1.0);
    const double linear = e <= 0.5 ? e * e / 3 : (std::exp((e - c) / a) + b) / 12;
    return 1000 * linear;
}

// Extended Reinhard: 203-nit diffuse white to 1.0, `peak` nits compressed to 1.0.
double tone_map(double nits, double peak = 1000.0) {
    const double x = nits / sdr_white_nits;
    const double w = peak / sdr_white_nits;
    return x * (1 + x / (w * w)) / (1 + x);
}

double srgb_encode(double linear) {
    linear = std::clamp(linear, 0.0, 1.0);
    return linear <= 0.0031308 ? 12.92 * linear : 1.055 * std::pow(linear, 1 / 2.4) - 0.055;
}

// numpy.linspace(0, 1, lut_size)[i].
double grid(int i) { return i == lut_size - 1 ? 1.0 : i * (1.0 / (lut_size - 1)); }

std::uint32_t rgb_word(std::uint8_t r, std::uint8_t g, std::uint8_t b) {
    return 0xFF000000u | (std::uint32_t{r} << 16) | (std::uint32_t{g} << 8) | b;
}

// float32 in [0, 1] to 8 bits, as numpy's (clip(x, 0, 1) * 255 + 0.5).astype(uint8).
std::uint8_t to_8bit(float value) {
    return static_cast<std::uint8_t>(std::clamp(value, 0.0f, 1.0f) * 255.0f + 0.5f);
}

int lut_index(float value) {
    const float scaled = value * static_cast<float>(lut_size - 1) + 0.5f;
    return static_cast<int>(std::clamp(scaled, 0.0f, static_cast<float>(lut_size - 1)));
}

// Splits [0, height) into row bands and runs `convert(first, end)` on each,
// in parallel when the frame is large.
template <typename Convert>
void in_bands(int height, std::size_t samples, Convert convert) {
    if (samples < band_min_samples) {
        convert(0, height);
        return;
    }
    const int rows = (height + band_count - 1) / band_count;
    std::vector<int> firsts;
    for (int first = 0; first < height; first += rows) {
        firsts.push_back(first);
    }
    std::for_each(std::execution::par, firsts.begin(), firsts.end(),
                  [&](int first) { convert(first, std::min(height, first + rows)); });
}

}  // namespace

Converter::Converter(DisplayFormat format) : format_(std::move(format)) {
    auto known = matrices.find(format_.matrix);
    if (known == matrices.end()) {
        known = matrices.find(format_.height > 576 ? "bt709" : "smpte170m");
    }
    const auto [kr, kb] = known->second;
    kr_ = static_cast<float>(kr);
    kb_ = static_cast<float>(kb);
    kg_ = static_cast<float>(1 - kr - kb);
    r_from_v_ = static_cast<float>(2 * (1 - kr));
    b_from_u_ = static_cast<float>(2 * (1 - kb));
    hdr_ = format_.transfer == "smpte2084" || format_.transfer == "arib-std-b67";
    if (hdr_) {
        const bool pq = format_.transfer == "smpte2084";
        to_linear_.resize(lut_size);
        to_srgb_.resize(lut_size);
        for (int i = 0; i < lut_size; ++i) {
            const double nits = pq ? pq_to_nits(grid(i)) : hlg_to_nits(grid(i));
            to_linear_[static_cast<std::size_t>(i)] = static_cast<float>(tone_map(nits));
            to_srgb_[static_cast<std::size_t>(i)] = static_cast<std::uint8_t>(srgb_encode(grid(i)) * 255 + 0.5);
        }
        bt2020_to_709_ = format_.primaries.starts_with("bt2020");
    }
}

void Converter::to_rgb(const Frame& frame, RgbTarget target) const {
    in_bands(frame.geometry.height, frame.y.samples.size(),
             [&](int first, int end) { convert_rows(frame, first, end, target); });
}

void Converter::convert_rows(const Frame& frame, int first_row, int end_row, RgbTarget target) const {
    const Geometry& g = frame.geometry;
    // Chroma subsampling from the planes' sizes, as numpy.repeat upsampled them.
    const int sy = static_cast<int>(std::lround(static_cast<double>(frame.y.height) / frame.u.height));
    const int sx = static_cast<int>(std::lround(static_cast<double>(frame.y.width) / frame.u.width));
    const double scale = 1 << (g.bit_depth - 8);
    const auto center = static_cast<float>(128 * scale);
    const auto black = static_cast<float>(16 * scale);
    const auto luma_span = static_cast<float>(219 * scale);
    const auto chroma_span = static_cast<float>(224 * scale);
    const auto peak = static_cast<float>((1 << g.bit_depth) - 1);

    for (int y = first_row; y < end_row; ++y) {
        const std::uint16_t* luma = frame.y.row(y);
        const std::uint16_t* cb = frame.u.row(y / sy);
        const std::uint16_t* cr = frame.v.row(y / sy);
        std::uint32_t* out = target.pixels + static_cast<std::ptrdiff_t>(y) * target.stride;
        for (int x = 0; x < g.width; ++x) {
            float yf = luma[x];
            float uf = static_cast<float>(cb[x / sx]) - center;
            float vf = static_cast<float>(cr[x / sx]) - center;
            if (format_.limited_range) {
                yf = (yf - black) / luma_span;
                uf /= chroma_span;
                vf /= chroma_span;
            } else {
                yf /= peak;
                uf /= peak;
                vf /= peak;
            }
            const float r = yf + r_from_v_ * vf;
            const float b = yf + b_from_u_ * uf;
            const float gr = (yf - kr_ * r - kb_ * b) / kg_;
            if (!hdr_) {
                out[x] = rgb_word(to_8bit(r), to_8bit(gr), to_8bit(b));
                continue;
            }
            float lr = to_linear_[static_cast<std::size_t>(lut_index(r))];
            float lg = to_linear_[static_cast<std::size_t>(lut_index(gr))];
            float lb = to_linear_[static_cast<std::size_t>(lut_index(b))];
            if (bt2020_to_709_) {
                // A chain of fused multiply-adds, rounded as numpy's float32
                // matrix product rounded them.
                const auto row = [&](const float (&m)[3]) {
                    return std::fma(lb, m[2], std::fma(lg, m[1], lr * m[0]));
                };
                const float r709 = row(bt2020_to_709[0]);
                const float g709 = row(bt2020_to_709[1]);
                const float b709 = row(bt2020_to_709[2]);
                lr = r709;
                lg = g709;
                lb = b709;
            }
            out[x] = rgb_word(to_srgb_[static_cast<std::size_t>(lut_index(lr))],
                              to_srgb_[static_cast<std::size_t>(lut_index(lg))],
                              to_srgb_[static_cast<std::size_t>(lut_index(lb))]);
        }
    }
}

void grain_only(const Plane& clean, const Plane& grained, int bit_depth, RgbTarget target, float gain) {
    const auto unit = static_cast<float>(1 << (bit_depth - 8));
    in_bands(clean.height, clean.samples.size(), [&](int first, int end) {
        for (int y = first; y < end; ++y) {
            const std::uint16_t* before = clean.row(y);
            const std::uint16_t* after = grained.row(y);
            std::uint32_t* out = target.pixels + static_cast<std::ptrdiff_t>(y) * target.stride;
            for (int x = 0; x < clean.width; ++x) {
                const float diff = (static_cast<float>(after[x]) - static_cast<float>(before[x])) / unit;
                const auto grey = static_cast<std::uint8_t>(std::clamp(128.0f + diff * gain, 0.0f, 255.0f));
                out[x] = rgb_word(grey, grey, grey);
            }
        }
    });
}

}  // namespace fgw
