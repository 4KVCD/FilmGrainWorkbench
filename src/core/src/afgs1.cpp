#include "fgw/afgs1.h"

#include <algorithm>
#include <bit>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>

#include "fgw/bits.h"

namespace fgw {

namespace {

const std::vector<ScalingPoint> silent_chroma = {{0, 0}};

int bits_for(int value) { return std::max(1, static_cast<int>(std::bit_width(static_cast<unsigned>(value)))); }

// log2 of the units the picture size is coded in (12 bits each).
int resolution_units(int width, int height) {
    for (int log2 = 0; log2 < 16; ++log2) {
        const int w = width >> log2;
        const int h = height >> log2;
        if (w <= 4095 && h <= 4095) {
            if ((w << log2) != width || (h << log2) != height) {
                throw std::invalid_argument(std::to_string(width) + "x" + std::to_string(height) +
                                            " cannot be signalled exactly in AFGS1 (needs units of " +
                                            std::to_string(1 << log2) + " samples)");
            }
            return log2;
        }
    }
    throw std::invalid_argument("picture too large for AFGS1");
}

void write_points(BitWriter& w, const std::vector<ScalingPoint>& points, bool chroma) {
    w.put(static_cast<std::uint32_t>(points.size()), 4);
    if (points.empty()) {
        return;
    }
    std::vector<int> increments;
    int bits_increment = 1;
    for (std::size_t i = 0; i < points.size(); ++i) {
        increments.push_back(i == 0 ? points[0].x : points[i].x - points[i - 1].x);
        bits_increment = std::max(bits_increment, bits_for(increments.back()));
    }
    int offset = 0;
    if (chroma) {
        offset = std::min_element(points.begin(), points.end(), [](const auto& a, const auto& b) {
                     return a.scaling < b.scaling;
                 })->scaling;
    }
    int bits_scaling = 5;
    for (const ScalingPoint& point : points) {
        bits_scaling = std::max(bits_scaling, bits_for(point.scaling - offset));
    }
    w.put(static_cast<std::uint32_t>(bits_increment - 1), 3);
    w.put(static_cast<std::uint32_t>(bits_scaling - 5), 2);
    if (chroma) {
        w.put(static_cast<std::uint32_t>(offset), 8);
    }
    for (std::size_t i = 0; i < points.size(); ++i) {
        w.put(static_cast<std::uint32_t>(increments[i]), bits_increment);
        w.put(static_cast<std::uint32_t>(points[i].scaling - offset), bits_scaling);
    }
}

void write_ar(BitWriter& w, const std::vector<int>& coeffs) {
    int bits = 5;
    const auto fits = [&bits](int c) { return -(1 << (bits - 1)) <= c && c < (1 << (bits - 1)); };
    while (!std::all_of(coeffs.begin(), coeffs.end(), fits)) {
        ++bits;
    }
    w.put(static_cast<std::uint32_t>(bits - 5), 2);
    for (const int c : coeffs) {
        w.put(static_cast<std::uint32_t>(c + (1 << (bits - 1))), bits);
    }
}

// `coeffs` cut or padded with zeros to `count`.
std::vector<int> resized(const std::vector<int>& coeffs, int count) {
    std::vector<int> out(coeffs.begin(), coeffs.begin() + std::min<std::ptrdiff_t>(coeffs.size(), count));
    out.resize(static_cast<std::size_t>(count), 0);
    return out;
}

BitWriter film_grain_params_bits(const GrainParams& p, const PictureFormat& format, int seed) {
    BitWriter w;
    w.put(0, 3);  // film_grain_param_set_idx
    w.put(p.apply_grain ? 1 : 0, 1);
    if (!p.apply_grain) {
        return w;
    }
    w.put(static_cast<std::uint32_t>(seed) & 0xFFFFu, 16);  // grain_seed
    w.put(1, 1);                                            // update_grain_flag
    const int log2 = resolution_units(format.width, format.height);
    w.put(static_cast<std::uint32_t>(log2), 4);
    w.put(static_cast<std::uint32_t>(format.width >> log2), 12);
    w.put(static_cast<std::uint32_t>(format.height >> log2), 12);
    w.put(format.monochrome ? 1 : 0, 1);  // luma_only_flag
    if (!format.monochrome) {
        w.put(static_cast<std::uint32_t>(format.subsampling_x), 1);
        w.put(static_cast<std::uint32_t>(format.subsampling_y), 1);
    }
    w.put(1, 1);  // video_signal_characteristics_flag
    w.put(static_cast<std::uint32_t>(format.bit_depth - 8), 3);
    w.put(0, 1);  // cicp_info_present_flag
    w.put(0, 1);  // predict_scaling_flag
    write_points(w, p.y_points, false);

    const bool from_luma = p.chroma_scaling_from_luma && !format.monochrome;
    const std::vector<ScalingPoint>* cb_points = nullptr;
    const std::vector<ScalingPoint>* cr_points = nullptr;
    if (!format.monochrome) {
        w.put(from_luma ? 1 : 0, 1);
        if (!from_luma) {
            cb_points = p.cb_points.empty() ? &silent_chroma : &p.cb_points;
            cr_points = p.cr_points.empty() ? &silent_chroma : &p.cr_points;
            write_points(w, *cb_points, true);
            write_points(w, *cr_points, true);
        }
    }

    w.put(static_cast<std::uint32_t>(p.scaling_shift - 8), 2);  // grain_scaling_minus8
    w.put(static_cast<std::uint32_t>(p.ar_coeff_lag), 2);
    const int luma_count = luma_ar_coefficient_count(p.ar_coeff_lag);
    const int chroma_count = p.chroma_ar_coefficient_count();
    if (!p.y_points.empty()) {
        write_ar(w, resized(p.ar_coeffs_y, std::min<int>(luma_count, static_cast<int>(p.ar_coeffs_y.size()))));
    }
    if (from_luma || cb_points != nullptr) {
        write_ar(w, resized(p.ar_coeffs_cb, chroma_count));
    }
    if (from_luma || cr_points != nullptr) {
        write_ar(w, resized(p.ar_coeffs_cr, chroma_count));
    }
    w.put(static_cast<std::uint32_t>(p.ar_coeff_shift - 6), 2);
    w.put(static_cast<std::uint32_t>(p.grain_scale_shift), 2);
    if (cb_points != nullptr) {
        w.put(static_cast<std::uint32_t>(p.cb_mult), 8);
        w.put(static_cast<std::uint32_t>(p.cb_luma_mult), 8);
        w.put(static_cast<std::uint32_t>(p.cb_offset), 9);
    }
    if (cr_points != nullptr) {
        w.put(static_cast<std::uint32_t>(p.cr_mult), 8);
        w.put(static_cast<std::uint32_t>(p.cr_luma_mult), 8);
        w.put(static_cast<std::uint32_t>(p.cr_offset), 9);
    }
    w.put(p.overlap_flag ? 1 : 0, 1);
    w.put(p.clip_to_restricted_range ? 1 : 0, 1);
    return w;
}

// grain_seed starts after: the 32 prefix bits, 8 bits of set header, then
// payload_less_than_4byte_flag (1) + payload_size (8) + set_idx (3) + apply (1).
constexpr std::size_t seed_bit = 8 * t35_prefix.size() + 8 + 13;

}  // namespace

std::vector<std::uint8_t> encode_t35(const GrainParams& params, const PictureFormat& format, int seed) {
    params.validate(format.subsampling_x == 1 && format.subsampling_y == 1);
    const BitWriter set_bits = film_grain_params_bits(params, format, seed);
    const std::size_t size_bytes = (1 + 8 + set_bits.size() + 7) / 8;
    if (size_bytes > 255) {
        throw std::invalid_argument("film grain payload exceeds 255 bytes");
    }
    BitWriter payload;
    payload.put(0, 1);  // payload_less_than_4byte_flag
    payload.put(static_cast<std::uint32_t>(size_bytes), 8);
    payload.put_bits(set_bits);
    payload.pad_to(size_bytes * 8);

    BitWriter sets;
    sets.put(1, 1);  // afgs1_enable_flag
    sets.put(0, 4);  // reserved_4bits
    sets.put(0, 3);  // num_film_grain_sets_minus1
    sets.put_bits(payload);
    std::vector<std::uint8_t> out(t35_prefix.begin(), t35_prefix.end());
    out.insert(out.end(), sets.bytes().begin(), sets.bytes().end());
    return out;
}

T35Template::T35Template(const GrainParams& params, const PictureFormat& format)
    : payload_(encode_t35(params, format, 0)), apply_grain_(params.apply_grain) {}

std::vector<std::uint8_t> T35Template::for_seed(int seed) const {
    std::vector<std::uint8_t> out = payload_;
    if (!apply_grain_) {
        return out;
    }
    const auto value = static_cast<std::uint32_t>(seed) & 0xFFFFu;
    for (int bit = 0; bit < 16; ++bit) {
        const std::size_t at = seed_bit + static_cast<std::size_t>(bit);
        if ((value >> (15 - bit)) & 1u) {
            out[at / 8] |= static_cast<std::uint8_t>(0x80u >> (at % 8));
        }
    }
    return out;
}

int picture_seed(int base_seed, std::int64_t picture_index) {
    // Each base seed's sequence, kept: writing a film asks for every picture
    // in turn, and working each out from the start would take quadratic time.
    static std::mutex mutex;
    static std::map<int, std::vector<std::uint16_t>> sequences;
    const std::lock_guard lock(mutex);
    const int base = base_seed & 0xFFFF;
    std::vector<std::uint16_t>& sequence = sequences[base];
    if (sequence.empty()) {
        sequence.push_back(static_cast<std::uint16_t>(base != 0 ? base : libaom_seed));
    }
    const auto index = static_cast<std::size_t>(std::max<std::int64_t>(picture_index, 0));
    while (sequence.size() <= index) {
        const int next = (sequence.back() + libaom_seed_step) & 0xFFFF;
        sequence.push_back(static_cast<std::uint16_t>(next != 0 ? next : libaom_seed));
    }
    return sequence[index];
}

namespace {

std::vector<ScalingPoint> read_points(BitReader& r, bool chroma) {
    const std::uint32_t count = r.get(4);
    if (count == 0) {
        return {};
    }
    const int bits_increment = static_cast<int>(r.get(3)) + 1;
    const int bits_scaling = static_cast<int>(r.get(2)) + 5;
    const int offset = chroma ? static_cast<int>(r.get(8)) : 0;
    std::vector<ScalingPoint> points;
    int x = 0;
    for (std::uint32_t i = 0; i < count; ++i) {
        const auto increment = static_cast<int>(r.get(bits_increment));
        x = i == 0 ? increment : x + increment;
        points.push_back({x, static_cast<int>(r.get(bits_scaling)) + offset});
    }
    return points;
}

std::vector<int> read_ar(BitReader& r, int count) {
    const int bits = static_cast<int>(r.get(2)) + 5;
    std::vector<int> coeffs;
    for (int i = 0; i < count; ++i) {
        coeffs.push_back(static_cast<int>(r.get(bits)) - (1 << (bits - 1)));
    }
    return coeffs;
}

DecodedSet read_set_contents(BitReader& r) {
    DecodedSet set;
    GrainParams& p = set.params;
    p.apply_grain = r.get(1) != 0;
    if (!p.apply_grain) {
        return set;
    }
    set.seed = static_cast<int>(r.get(16));
    if (r.get(1) == 0) {
        throw std::invalid_argument("update_grain_flag = 0 is not supported");
    }
    const int log2 = static_cast<int>(r.get(4));
    set.format.width = static_cast<int>(r.get(12)) << log2;
    set.format.height = static_cast<int>(r.get(12)) << log2;
    set.format.monochrome = r.get(1) != 0;
    set.format.subsampling_x = set.format.subsampling_y = 0;
    if (!set.format.monochrome) {
        set.format.subsampling_x = static_cast<int>(r.get(1));
        set.format.subsampling_y = static_cast<int>(r.get(1));
    }
    set.format.bit_depth = 0;
    if (r.get(1) != 0) {
        set.format.bit_depth = static_cast<int>(r.get(3)) + 8;
        if (r.get(1) != 0) {
            r.skip(8 + 8 + 8 + 1);  // colour primaries, transfer, matrix, full range
        }
    }
    if (r.get(1) != 0) {
        throw std::invalid_argument("predicted scaling functions are not supported");
    }
    p.y_points = read_points(r, false);
    if (!set.format.monochrome) {
        p.chroma_scaling_from_luma = r.get(1) != 0;
        if (!p.chroma_scaling_from_luma) {
            p.cb_points = read_points(r, true);
            p.cr_points = read_points(r, true);
        }
    }
    p.scaling_shift = static_cast<int>(r.get(2)) + 8;
    p.ar_coeff_lag = static_cast<int>(r.get(2));
    const int chroma_count = p.chroma_ar_coefficient_count();
    if (!p.y_points.empty()) {
        p.ar_coeffs_y = read_ar(r, luma_ar_coefficient_count(p.ar_coeff_lag));
    }
    if (p.chroma_scaling_from_luma || !p.cb_points.empty()) {
        p.ar_coeffs_cb = read_ar(r, chroma_count);
    }
    if (p.chroma_scaling_from_luma || !p.cr_points.empty()) {
        p.ar_coeffs_cr = read_ar(r, chroma_count);
    }
    p.ar_coeff_shift = static_cast<int>(r.get(2)) + 6;
    p.grain_scale_shift = static_cast<int>(r.get(2));
    if (!p.cb_points.empty()) {
        p.cb_mult = static_cast<int>(r.get(8));
        p.cb_luma_mult = static_cast<int>(r.get(8));
        p.cb_offset = static_cast<int>(r.get(9));
    }
    if (!p.cr_points.empty()) {
        p.cr_mult = static_cast<int>(r.get(8));
        p.cr_luma_mult = static_cast<int>(r.get(8));
        p.cr_offset = static_cast<int>(r.get(9));
    }
    p.overlap_flag = r.get(1) != 0;
    p.clip_to_restricted_range = r.get(1) != 0;
    return set;
}

}  // namespace

std::vector<DecodedSet> decode_t35(std::span<const std::uint8_t> data) {
    if (data.size() < t35_prefix.size() || !std::equal(t35_prefix.begin(), t35_prefix.end(), data.begin())) {
        throw std::invalid_argument("not an AOMedia AFGS1 T.35 payload");
    }
    try {
        BitReader r(data.subspan(t35_prefix.size()));
        if (r.get(1) == 0) {
            return {};
        }
        r.skip(4);
        const std::uint32_t count = r.get(3) + 1;
        std::vector<DecodedSet> out;
        for (std::uint32_t i = 0; i < count; ++i) {
            const std::size_t start = r.position();
            const bool short_size = r.get(1) != 0;
            const std::size_t size = r.get(short_size ? 2 : 8);
            r.skip(3);  // film_grain_param_set_idx
            DecodedSet set = read_set_contents(r);
            const std::size_t used = r.position() - start;
            if (used > size * 8) {
                throw std::invalid_argument("film grain parameters overrun payload_size");
            }
            r.skip(size * 8 - used);
            set.params.random_seed = set.seed;
            out.push_back(std::move(set));
        }
        return out;
    } catch (const std::out_of_range& error) {
        throw std::invalid_argument(std::string("AFGS1 payload ends early: ") + error.what());
    }
}

}  // namespace fgw
