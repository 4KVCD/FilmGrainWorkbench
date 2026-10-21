#include "fgw/synth.h"

#include <algorithm>
#include <cmath>
#include <cstdint>

#include "gaussian.h"

namespace fgw {

namespace {

// The specification's Round2: x / 2^n rounded, halves up (arithmetic shift).
constexpr std::int64_t round2(std::int64_t x, int n) { return n == 0 ? x : (x + (std::int64_t{1} << (n - 1))) >> n; }

constexpr int clip(std::int64_t value, int low, int high) {
    return static_cast<int>(value < low ? low : value > high ? high : value);
}

// The specification's 16-bit RandomRegister and get_random_number().
class RandomRegister {
public:
    explicit RandomRegister(int seed) : r_(static_cast<unsigned>(seed) & 0xFFFFu) {}
    void mix(unsigned value) { r_ ^= value; }
    int next(int bits) {
        const unsigned bit = (r_ ^ (r_ >> 1) ^ (r_ >> 3) ^ (r_ >> 12)) & 1u;
        r_ = (r_ >> 1) | (bit << 15);
        return static_cast<int>((r_ >> (16 - bits)) & ((1u << bits) - 1));
    }

private:
    unsigned r_;
};

// The noise range at a bit depth: [-center, 2^bit_depth - 1 - center].
struct GrainRange {
    int min;
    int max;
    explicit GrainRange(int bit_depth) : min(-(128 << (bit_depth - 8))), max((256 << (bit_depth - 8)) - 1 + min) {}
};

std::vector<int> white_noise(int seed, int height, int width, int shift) {
    RandomRegister random(seed);
    std::vector<int> noise(static_cast<std::size_t>(height) * width);
    for (int& sample : noise) {
        sample = static_cast<int>(round2(gaussian_sequence[static_cast<std::size_t>(random.next(11))], shift));
    }
    return noise;
}

// Spec 8.2.3's causal auto-regressive filter, in place. `luma_term` holds,
// per filtered position ((height - 3) × (width - 6)), the averaged luma grain
// that the last coefficient multiplies (chroma only).
void ar_filter(std::vector<int>& grain, int height, int width, const std::vector<int>& coeffs, int lag, int shift,
               GrainRange range, const std::vector<int>* luma_term) {
    for (int y = 3; y < height; ++y) {
        for (int x = 3; x < width - 3; ++x) {
            std::int64_t sum = 0;
            std::size_t c = 0;
            for (int dy = -lag; dy <= 0; ++dy) {
                for (int dx = -lag; dx <= lag; ++dx) {
                    if (dy == 0 && dx == 0) {
                        break;
                    }
                    sum += std::int64_t{coeffs[c++]} * grain[static_cast<std::size_t>(y + dy) * width + x + dx];
                }
            }
            if (luma_term != nullptr) {
                sum += std::int64_t{coeffs[c]} * (*luma_term)[static_cast<std::size_t>(y - 3) * (width - 6) + x - 3];
            }
            int& sample = grain[static_cast<std::size_t>(y) * width + x];
            sample = clip(sample + round2(sum, shift), range.min, range.max);
        }
    }
}

// The luma grain averaged to each filtered chroma template position.
std::vector<int> luma_for_chroma(const std::vector<int>& luma, int chroma_height, int chroma_width, int subx,
                                 int suby) {
    std::vector<int> term(static_cast<std::size_t>(chroma_height - 3) * (chroma_width - 6));
    for (int y = 3; y < chroma_height; ++y) {
        for (int x = 3; x < chroma_width - 3; ++x) {
            const int luma_y = ((y - 3) << suby) + 3;
            const int luma_x = ((x - 3) << subx) + 3;
            std::int64_t sum = 0;
            for (int i = 0; i <= suby; ++i) {
                for (int j = 0; j <= subx; ++j) {
                    sum += luma[static_cast<std::size_t>(luma_y + i) * GrainTemplates::luma_width + luma_x + j];
                }
            }
            term[static_cast<std::size_t>(y - 3) * (chroma_width - 6) + x - 3] =
                static_cast<int>(round2(sum, subx + suby));
        }
    }
    return term;
}

}  // namespace

GrainTemplates generate_grain(const GrainParams& p, int seed, int bit_depth, int subx, int suby) {
    const GrainRange range(bit_depth);
    const int shift = 12 - bit_depth + p.grain_scale_shift;
    const int lag = p.ar_coeff_lag;
    GrainTemplates t;
    constexpr int lh = GrainTemplates::luma_height;
    constexpr int lw = GrainTemplates::luma_width;
    if (!p.y_points.empty()) {
        t.luma = white_noise(seed, lh, lw, shift);
        ar_filter(t.luma, lh, lw, p.ar_coeffs_y, lag, p.ar_coeff_shift, range, nullptr);
    } else {
        t.luma.assign(static_cast<std::size_t>(lh) * lw, 0);
    }

    t.chroma_height = suby ? 38 : 73;
    t.chroma_width = subx ? 44 : 82;
    std::vector<int> luma_term;
    if (!p.y_points.empty()) {
        luma_term = luma_for_chroma(t.luma, t.chroma_height, t.chroma_width, subx, suby);
    }
    const auto chroma_count = static_cast<std::size_t>(p.chroma_ar_coefficient_count());
    const auto make_chroma = [&](unsigned xor_seed, const std::vector<ScalingPoint>& points,
                                 const std::vector<int>& coeffs) {
        if (points.empty() && !p.chroma_scaling_from_luma) {
            return std::vector<int>(static_cast<std::size_t>(t.chroma_height) * t.chroma_width, 0);
        }
        std::vector<int> grain =
            white_noise(static_cast<int>((static_cast<unsigned>(seed) ^ xor_seed) & 0xFFFFu), t.chroma_height,
                        t.chroma_width, shift);
        std::vector<int> padded(coeffs.begin(), coeffs.begin() + std::min(coeffs.size(), chroma_count));
        padded.resize(chroma_count, 0);
        ar_filter(grain, t.chroma_height, t.chroma_width, padded, lag, p.ar_coeff_shift, range,
                  luma_term.empty() ? nullptr : &luma_term);
        return grain;
    };
    t.cb = make_chroma(0xB524u, p.cb_points, p.ar_coeffs_cb);
    t.cr = make_chroma(0x49D8u, p.cr_points, p.ar_coeffs_cr);
    return t;
}

std::array<int, 256> scaling_lut(const std::vector<ScalingPoint>& points) {
    std::array<int, 256> lut{};
    if (points.empty()) {
        return lut;
    }
    std::fill(lut.begin(), lut.begin() + points.front().x, points.front().scaling);
    for (std::size_t i = 0; i + 1 < points.size(); ++i) {
        const int dx = points[i + 1].x - points[i].x;
        const int dy = points[i + 1].scaling - points[i].scaling;
        const int delta = dy * ((65536 + (dx >> 1)) / dx);
        for (int x = 0; x < dx; ++x) {
            lut[static_cast<std::size_t>(points[i].x + x)] = points[i].scaling + ((x * delta + 32768) >> 16);
        }
    }
    std::fill(lut.begin() + points.back().x, lut.end(), points.back().scaling);
    return lut;
}

namespace {

// scale_lut() of the specification for every sample value of `bit_depth`.
std::vector<int> full_lut(const std::array<int, 256>& lut8, int bit_depth) {
    const int shift = bit_depth - 8;
    std::vector<int> out(std::size_t{1} << bit_depth);
    for (std::size_t index = 0; index < out.size(); ++index) {
        const auto x = static_cast<int>(index >> shift);
        if (shift == 0 || x == 255) {
            out[index] = lut8[static_cast<std::size_t>(x)];
            continue;
        }
        const int remainder = static_cast<int>(index) - (x << shift);
        const int start = lut8[static_cast<std::size_t>(x)];
        const int end = lut8[static_cast<std::size_t>(x + 1)];
        out[index] = start + static_cast<int>(round2(std::int64_t{end - start} * remainder, shift));
    }
    return out;
}

// One plane's grain, made a stripe at a time (spec 8.2.5): a stripe holds
// the 34 rows (fewer for subsampled chroma) of the 32×32 grain blocks placed
// along 32 picture rows, which overlap the next stripe by two rows.
struct PlaneNoise {
    int subx = 0;
    int suby = 0;
    const std::vector<int>* noise_template = nullptr;
    int template_width = 0;
    int rows = 0;   // in a stripe buffer
    int width = 0;  // of a stripe buffer
    std::vector<int> stripe;
    std::vector<int> previous;  // the stripe above, for the vertical overlap

    int* stripe_row(int y) { return stripe.data() + static_cast<std::size_t>(y) * width; }
    const int* previous_row(int y) const { return previous.data() + static_cast<std::size_t>(y) * width; }
};

// Places the grain blocks of stripe `index` (spec 8.2.5's luma_num) in
// every plane's stripe buffer.
void build_stripes(std::vector<PlaneNoise>& planes, const GrainParams& p, int seed, int index,
                   int blocks_per_stripe, GrainRange range) {
    RandomRegister random(seed);
    random.mix(static_cast<unsigned>((index * 37 + 178) & 255) << 8);
    random.mix(static_cast<unsigned>((index * 173 + 105) & 255));
    for (PlaneNoise& plane : planes) {
        plane.previous.swap(plane.stripe);
        plane.stripe.assign(static_cast<std::size_t>(plane.rows) * plane.width, 0);
    }
    for (int block = 0; block < blocks_per_stripe; ++block) {
        const int x = block * 16;
        const int offsets = random.next(8);
        const int offset_x = offsets >> 4;
        const int offset_y = offsets & 15;
        for (PlaneNoise& plane : planes) {
            const int ox = plane.subx ? 6 + offset_x : 9 + offset_x * 2;
            const int oy = plane.suby ? 6 + offset_y : 9 + offset_y * 2;
            const int block_height = 34 >> plane.suby;
            const int block_width = 34 >> plane.subx;
            const int column = plane.subx ? x : x * 2;
            const bool overlap = p.overlap_flag && x > 0;
            for (int row = 0; row < block_height; ++row) {
                const int* source = plane.noise_template->data() +
                                    static_cast<std::size_t>(oy + row) * plane.template_width + ox;
                int* dest = plane.stripe_row(row) + column;
                for (int col = 0; col < block_width; ++col) {
                    int value = source[col];
                    if (overlap && plane.subx == 0 && col < 2) {
                        // Blend with the block to the left: 27/17 and 17/27 over two columns.
                        const int old_weight = col == 0 ? 27 : 17;
                        value = clip(round2(dest[col] * old_weight + value * (44 - old_weight), 5), range.min,
                                     range.max);
                    } else if (overlap && plane.subx == 1 && col == 0) {
                        value = clip(round2(dest[col] * 23 + value * 22, 5), range.min, range.max);
                    }
                    dest[col] = value;
                }
            }
        }
    }
}

// Row `row` of a plane's current stripe as noise for the picture, blended
// with the stripe above (spec 8.2.5's vertical overlap) into `out`.
const int* noise_row(const PlaneNoise& plane, int row, bool overlap_above, int count, GrainRange range,
                     std::vector<int>& out) {
    const int* current = plane.stripe.data() + static_cast<std::size_t>(row) * plane.width;
    if (!overlap_above) {
        return current;
    }
    int above_row = 0;
    int above_weight = 0;
    if (plane.suby == 0 && row < 2) {
        above_row = 32 + row;
        above_weight = row == 0 ? 27 : 17;
    } else if (plane.suby == 1 && row == 0) {
        above_row = 16;
        above_weight = 23;
    } else {
        return current;
    }
    const int current_weight = plane.suby == 0 ? 44 - above_weight : 22;
    const int* above = plane.previous_row(above_row);
    out.resize(static_cast<std::size_t>(count));
    for (int x = 0; x < count; ++x) {
        out[static_cast<std::size_t>(x)] =
            clip(round2(std::int64_t{above[x]} * above_weight + std::int64_t{current[x]} * current_weight, 5),
                 range.min, range.max);
    }
    return out.data();
}

// The sample range grained pictures are clipped to.
struct OutputRange {
    int min;
    int max_luma;
    int max_chroma;
};

OutputRange output_range(const GrainParams& p, int bit_depth, bool matrix_identity) {
    if (p.clip_to_restricted_range) {
        const int max_luma = 235 << (bit_depth - 8);
        return {16 << (bit_depth - 8), max_luma, matrix_identity ? max_luma : 240 << (bit_depth - 8)};
    }
    const int peak = (256 << (bit_depth - 8)) - 1;
    return {0, peak, peak};
}

}  // namespace

void add_grain(Frame& frame, const GrainParams& p, int seed, bool matrix_identity) {
    if (!p.apply_grain) {
        return;
    }
    const Geometry& g = frame.geometry;
    const int bit_depth = g.bit_depth;
    const int subx = g.subsampling_x;
    const int suby = g.subsampling_y;
    const GrainRange range(bit_depth);
    const OutputRange out = output_range(p, bit_depth, matrix_identity);
    const GrainTemplates templates = generate_grain(p, seed, bit_depth, subx, suby);
    const int shift = p.scaling_shift;
    const int peak = (1 << bit_depth) - 1;
    const std::vector<int> luma_lut = full_lut(scaling_lut(p.y_points), bit_depth);
    const bool chroma_from_luma = p.chroma_scaling_from_luma;
    const std::vector<int> cb_lut = chroma_from_luma ? luma_lut : full_lut(scaling_lut(p.cb_points), bit_depth);
    const std::vector<int> cr_lut = chroma_from_luma ? luma_lut : full_lut(scaling_lut(p.cr_points), bit_depth);
    const bool grain_cb = chroma_from_luma || !p.cb_points.empty();
    const bool grain_cr = chroma_from_luma || !p.cr_points.empty();

    const int stripes = ((g.height + 1) / 2 + 15) / 16;
    const int blocks_per_stripe = ((g.width + 1) / 2 + 15) / 16;
    std::vector<PlaneNoise> planes(3);
    const std::vector<int>* noise_templates[3] = {&templates.luma, &templates.cb, &templates.cr};
    for (int i = 0; i < 3; ++i) {
        PlaneNoise& plane = planes[static_cast<std::size_t>(i)];
        plane.subx = i ? subx : 0;
        plane.suby = i ? suby : 0;
        plane.noise_template = noise_templates[i];
        plane.template_width = i ? templates.chroma_width : GrainTemplates::luma_width;
        plane.rows = 34 >> plane.suby;
        plane.width = ((blocks_per_stripe * 32) >> plane.subx) + 34;
    }

    std::vector<int> blended;
    for (int stripe = 0; stripe < stripes; ++stripe) {
        build_stripes(planes, p, seed, stripe, blocks_per_stripe, range);
        const bool overlap_above = p.overlap_flag && stripe > 0;

        // Chroma first: it is scaled by the luma samples before their grain.
        const int chroma_rows_per_stripe = 32 >> suby;
        const int chroma_first = stripe * chroma_rows_per_stripe;
        const int chroma_count = std::min(chroma_rows_per_stripe, frame.u.height - chroma_first);
        for (int plane_index = 1; plane_index <= 2; ++plane_index) {
            const bool cb = plane_index == 1;
            if (!(cb ? grain_cb : grain_cr)) {
                continue;
            }
            Plane& chroma = cb ? frame.u : frame.v;
            const std::vector<int>& lut = cb ? cb_lut : cr_lut;
            const int mult = (cb ? p.cb_mult : p.cr_mult) - 128;
            const int luma_mult = (cb ? p.cb_luma_mult : p.cr_luma_mult) - 128;
            const int offset = ((cb ? p.cb_offset : p.cr_offset) - 256) * (1 << (bit_depth - 8));
            const PlaneNoise& noise = planes[static_cast<std::size_t>(plane_index)];
            for (int row = 0; row < chroma_count; ++row) {
                const int cy = chroma_first + row;
                const int* n = noise_row(noise, row, overlap_above, chroma.width, range, blended);
                const std::uint16_t* luma_row = frame.y.row(cy << suby);
                std::uint16_t* samples = chroma.row(cy);
                for (int cx = 0; cx < chroma.width; ++cx) {
                    const int luma_x = cx << subx;
                    int average = luma_row[luma_x];
                    if (subx) {
                        average = static_cast<int>(round2(average + luma_row[std::min(luma_x + 1, g.width - 1)], 1));
                    }
                    int merged = average;
                    if (!chroma_from_luma) {
                        const int combined = average * luma_mult + samples[cx] * mult;
                        merged = clip((combined >> 6) + offset, 0, peak);
                    }
                    const auto grain = round2(std::int64_t{lut[static_cast<std::size_t>(merged)]} * n[cx], shift);
                    samples[cx] = static_cast<std::uint16_t>(clip(samples[cx] + grain, out.min, out.max_chroma));
                }
            }
        }

        if (!p.y_points.empty()) {
            const int first = stripe * 32;
            const int count = std::min(32, g.height - first);
            for (int row = 0; row < count; ++row) {
                const int* n = noise_row(planes[0], row, overlap_above, g.width, range, blended);
                std::uint16_t* samples = frame.y.row(first + row);
                for (int x = 0; x < g.width; ++x) {
                    const auto grain = round2(std::int64_t{luma_lut[samples[x]]} * n[x], shift);
                    samples[x] = static_cast<std::uint16_t>(clip(samples[x] + grain, out.min, out.max_luma));
                }
            }
        }
    }
}

namespace {

constexpr int template_std_seeds[] = {1, 7391, 40000};

// Population standard deviation of a rectangle of a template.
double region_std(const std::vector<int>& values, int width, int top, int left, int height, int region_width) {
    double sum = 0;
    for (int y = top; y < top + height; ++y) {
        for (int x = left; x < left + region_width; ++x) {
            sum += values[static_cast<std::size_t>(y) * width + x];
        }
    }
    const double count = static_cast<double>(height) * region_width;
    const double mean = sum / count;
    double squares = 0;
    for (int y = top; y < top + height; ++y) {
        for (int x = left; x < left + region_width; ++x) {
            const double d = values[static_cast<std::size_t>(y) * width + x] - mean;
            squares += d * d;
        }
    }
    return std::sqrt(squares / count);
}

}  // namespace

double luma_template_std(const GrainParams& params, int bit_depth, std::span<const int> seeds) {
    if (seeds.empty()) {
        seeds = template_std_seeds;
    }
    double total = 0;
    for (const int seed : seeds) {
        const GrainTemplates t = generate_grain(params, seed, bit_depth, 1, 1);
        total += region_std(t.luma, GrainTemplates::luma_width, 9, 9, 64, 64);
    }
    return total / static_cast<double>(seeds.size());
}

std::pair<double, double> chroma_template_std(const GrainParams& params, int bit_depth, int subx, int suby) {
    const int top = suby ? 6 : 9;
    const int left = subx ? 6 : 9;
    const int height = (34 >> suby) + 15;
    const int width = (34 >> subx) + 15;
    double cb = 0;
    double cr = 0;
    for (const int seed : template_std_seeds) {
        const GrainTemplates t = generate_grain(params, seed, bit_depth, subx, suby);
        cb += region_std(t.cb, t.chroma_width, top, left, height, width);
        cr += region_std(t.cr, t.chroma_width, top, left, height, width);
    }
    const double n = static_cast<double>(std::size(template_std_seeds));
    return {cb / n, cr / n};
}

}  // namespace fgw
