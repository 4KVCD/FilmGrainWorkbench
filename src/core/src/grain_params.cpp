#include "fgw/grain_params.h"

#include <algorithm>
#include <charconv>
#include <stdexcept>

namespace fgw {

namespace {

[[noreturn]] void invalid(const std::string& why) { throw std::invalid_argument(why); }

void check_points(const std::vector<ScalingPoint>& points, int limit, const std::string& name) {
    if (static_cast<int>(points.size()) > limit) {
        invalid(name + ": " + std::to_string(points.size()) + " points, at most " + std::to_string(limit) +
                " allowed");
    }
    int previous = -1;
    for (const ScalingPoint& point : points) {
        if (point.x < 0 || point.x > 255 || point.scaling < 0 || point.scaling > 255) {
            invalid(name + ": point (" + std::to_string(point.x) + ", " + std::to_string(point.scaling) +
                    ") outside 0..255");
        }
        if (point.x <= previous) {
            invalid(name + ": x coordinates must strictly increase");
        }
        previous = point.x;
    }
}

bool in_range(int value, int low, int high) { return low <= value && value <= high; }

}  // namespace

void GrainParams::validate(bool subsampled_420) const {
    if (!apply_grain) {
        return;
    }
    check_points(y_points, max_luma_points, "luma scaling");
    check_points(cb_points, max_chroma_points, "Cb scaling");
    check_points(cr_points, max_chroma_points, "Cr scaling");
    if (subsampled_420 && !chroma_scaling_from_luma && cb_points.empty() != cr_points.empty()) {
        invalid("4:2:0 grain needs both or neither of the Cb and Cr curves");
    }
    if (!in_range(scaling_shift, 8, 11)) invalid("scaling_shift must be 8..11");
    if (!in_range(ar_coeff_lag, 0, 3)) invalid("ar_coeff_lag must be 0..3");
    if (!in_range(ar_coeff_shift, 6, 9)) invalid("ar_coeff_shift must be 6..9");
    if (!in_range(grain_scale_shift, 0, 3)) invalid("grain_scale_shift must be 0..3");
    const int luma_count = luma_ar_coefficient_count(ar_coeff_lag);
    if (!y_points.empty() && static_cast<int>(ar_coeffs_y.size()) != luma_count) {
        invalid("expected " + std::to_string(luma_count) + " luma AR coefficients");
    }
    // Empty means "no chroma AR filtering" and is coded as zeros.
    const auto chroma_count = static_cast<std::size_t>(chroma_ar_coefficient_count());
    for (const auto* coeffs : {&ar_coeffs_cb, &ar_coeffs_cr}) {
        if (!coeffs->empty() && coeffs->size() != chroma_count) {
            invalid("expected " + std::to_string(chroma_count) + " values in " +
                    (coeffs == &ar_coeffs_cb ? "ar_coeffs_cb" : "ar_coeffs_cr"));
        }
    }
    for (const auto* coeffs : {&ar_coeffs_y, &ar_coeffs_cb, &ar_coeffs_cr}) {
        if (std::any_of(coeffs->begin(), coeffs->end(), [](int c) { return !in_range(c, -128, 127); })) {
            invalid("AR coefficients must be -128..127");
        }
    }
    for (const int multiplier : {cb_mult, cb_luma_mult, cr_mult, cr_luma_mult}) {
        if (!in_range(multiplier, 0, 255)) invalid("chroma multipliers must be 0..255");
    }
    for (const int offset : {cb_offset, cr_offset}) {
        if (!in_range(offset, 0, 511)) invalid("chroma offsets must be 0..511");
    }
}

namespace {

// The non-empty lines of a grain table, split into words.
class TableLines {
public:
    explicit TableLines(std::string_view text) {
        std::size_t start = 0;
        while (start <= text.size()) {
            std::size_t end = text.find('\n', start);
            if (end == std::string_view::npos) {
                end = text.size();
            }
            std::vector<std::string_view> words = split(text.substr(start, end - start));
            if (!words.empty()) {
                lines_.push_back(std::move(words));
            }
            start = end + 1;
        }
    }

    bool done() const { return next_ >= lines_.size(); }
    const std::vector<std::string_view>& next() { return lines_[next_++]; }
    const std::vector<std::string_view>& first() const { return lines_.front(); }
    bool empty() const { return lines_.empty(); }

    // The numbers of the next line, which must start with `prefix`.
    std::vector<int> take(std::string_view prefix) {
        if (done()) {
            invalid("grain table ends before its '" + std::string(prefix) + "' line");
        }
        const auto& words = next();
        if (words.front() != prefix) {
            invalid("expected a '" + std::string(prefix) + "' line, found '" + std::string(words.front()) + "'");
        }
        std::vector<int> numbers;
        numbers.reserve(words.size() - 1);
        for (std::size_t i = 1; i < words.size(); ++i) {
            numbers.push_back(to_int(words[i]));
        }
        return numbers;
    }

    std::vector<ScalingPoint> points(std::string_view prefix) {
        const std::vector<int> values = take(prefix);
        const int count = values.at(0);
        if (count < 0 || values.size() != static_cast<std::size_t>(1 + 2 * count)) {
            invalid("'" + std::string(prefix) + "' line has the wrong number of values");
        }
        std::vector<ScalingPoint> points(static_cast<std::size_t>(count));
        for (int k = 0; k < count; ++k) {
            points[static_cast<std::size_t>(k)] = {values[static_cast<std::size_t>(1 + 2 * k)],
                                                   values[static_cast<std::size_t>(2 + 2 * k)]};
        }
        return points;
    }

    static std::int64_t to_int64(std::string_view word) {
        std::int64_t value = 0;
        const auto [end, error] = std::from_chars(word.data(), word.data() + word.size(), value);
        if (error != std::errc() || end != word.data() + word.size()) {
            invalid("not a number in the grain table: '" + std::string(word) + "'");
        }
        return value;
    }

    static int to_int(std::string_view word) { return static_cast<int>(to_int64(word)); }

private:
    static std::vector<std::string_view> split(std::string_view line) {
        std::vector<std::string_view> words;
        std::size_t i = 0;
        while (i < line.size()) {
            while (i < line.size() && (line[i] == ' ' || line[i] == '\t' || line[i] == '\r')) {
                ++i;
            }
            const std::size_t start = i;
            while (i < line.size() && line[i] != ' ' && line[i] != '\t' && line[i] != '\r') {
                ++i;
            }
            if (i > start) {
                words.push_back(line.substr(start, i - start));
            }
        }
        return words;
    }

    std::vector<std::vector<std::string_view>> lines_;
    std::size_t next_ = 1;  // after "filmgrn1"
};

GrainParams parse_parameters(TableLines& lines) {
    const std::vector<int> p = lines.take("p");
    if (p.size() != 12) {
        invalid("'p' line must have 12 values");
    }
    GrainParams params;
    params.ar_coeff_lag = p[0];
    params.ar_coeff_shift = p[1];
    params.grain_scale_shift = p[2];
    params.scaling_shift = p[3];
    params.chroma_scaling_from_luma = p[4] != 0;
    params.overlap_flag = p[5] != 0;
    params.cb_mult = p[6];
    params.cb_luma_mult = p[7];
    params.cb_offset = p[8];
    params.cr_mult = p[9];
    params.cr_luma_mult = p[10];
    params.cr_offset = p[11];
    params.y_points = lines.points("sY");
    params.cb_points = lines.points("sCb");
    params.cr_points = lines.points("sCr");
    const auto luma_count = static_cast<std::size_t>(luma_ar_coefficient_count(params.ar_coeff_lag));
    params.ar_coeffs_y = lines.take("cY");
    // libaom always writes luma_count + 1 chroma coefficients; the last one
    // (the luma correlation) is only in the bitstream when there is luma grain.
    params.ar_coeffs_cb = lines.take("cCb");
    params.ar_coeffs_cr = lines.take("cCr");
    if (params.ar_coeffs_y.size() != luma_count || params.ar_coeffs_cb.size() != luma_count + 1 ||
        params.ar_coeffs_cr.size() != luma_count + 1) {
        invalid("AR coefficient count does not match ar_coeff_lag");
    }
    if (params.y_points.empty()) {
        params.ar_coeffs_cb.resize(luma_count);
        params.ar_coeffs_cr.resize(luma_count);
    }
    return params;
}

}  // namespace

std::vector<TableEntry> parse_grain_table(std::string_view text) {
    TableLines lines(text);
    if (lines.empty() || lines.first().size() != 1 || lines.first().front() != "filmgrn1") {
        invalid("not a filmgrn1 grain table");
    }
    std::vector<TableEntry> entries;
    while (!lines.done()) {
        const std::vector<std::string_view> head = lines.next();
        if (head.size() != 6 || head.front() != "E") {
            std::string shown;
            for (const auto word : head) {
                shown += (shown.empty() ? "" : " ") + std::string(word);
            }
            invalid("malformed grain table entry: " + shown);
        }
        TableEntry entry;
        entry.start = TableLines::to_int64(head[1]);
        entry.end = TableLines::to_int64(head[2]);
        const bool apply_grain = TableLines::to_int(head[3]) != 0;
        const int seed = TableLines::to_int(head[4]);
        const bool update = TableLines::to_int(head[5]) != 0;
        if (update) {
            entry.params = parse_parameters(lines);
        } else if (entries.empty()) {
            invalid("first grain table entry does not carry parameters");
        } else {
            entry.params = entries.back().params;
        }
        entry.params.apply_grain = apply_grain;
        entry.params.random_seed = seed & 0xFFFF;
        entries.push_back(std::move(entry));
    }
    if (entries.empty()) {
        invalid("grain table has no entries");
    }
    return entries;
}

namespace {

void append_points(std::string& out, const char* prefix, const std::vector<ScalingPoint>& points) {
    out += '\t';
    out += prefix;
    out += ' ';
    out += std::to_string(points.size());
    for (const ScalingPoint& point : points) {
        out += ' ' + std::to_string(point.x) + ' ' + std::to_string(point.scaling);
    }
    out += '\n';
}

void append_coefficients(std::string& out, const char* prefix, const std::vector<int>& coeffs,
                         std::size_t zeros_after) {
    out += '\t';
    out += prefix;
    for (const int c : coeffs) {
        out += ' ' + std::to_string(c);
    }
    for (std::size_t i = 0; i < zeros_after; ++i) {
        out += " 0";
    }
    out += '\n';
}

}  // namespace

std::string format_grain_table(const std::vector<TableEntry>& entries) {
    std::string out = "filmgrn1\n";
    for (const TableEntry& entry : entries) {
        const GrainParams& p = entry.params;
        out += "E " + std::to_string(entry.start) + ' ' + std::to_string(entry.end) + ' ' +
               std::to_string(p.apply_grain ? 1 : 0) + ' ' + std::to_string(p.random_seed) + " 1\n";
        out += "\tp";
        for (const int value : {p.ar_coeff_lag, p.ar_coeff_shift, p.grain_scale_shift, p.scaling_shift,
                                p.chroma_scaling_from_luma ? 1 : 0, p.overlap_flag ? 1 : 0, p.cb_mult,
                                p.cb_luma_mult, p.cb_offset, p.cr_mult, p.cr_luma_mult, p.cr_offset}) {
            out += ' ' + std::to_string(value);
        }
        out += '\n';
        append_points(out, "sY", p.y_points);
        append_points(out, "sCb", p.cb_points);
        append_points(out, "sCr", p.cr_points);
        // libaom writes luma_count + 1 chroma coefficients, zeros where a set has fewer.
        const auto chroma_written = static_cast<std::size_t>(luma_ar_coefficient_count(p.ar_coeff_lag) + 1);
        const auto padding = [chroma_written](const std::vector<int>& coeffs) {
            return chroma_written - std::min(chroma_written, coeffs.size());
        };
        append_coefficients(out, "cY", p.ar_coeffs_y, 0);
        append_coefficients(out, "cCb", p.ar_coeffs_cb, padding(p.ar_coeffs_cb));
        append_coefficients(out, "cCr", p.ar_coeffs_cr, padding(p.ar_coeffs_cr));
    }
    return out;
}

}  // namespace fgw
