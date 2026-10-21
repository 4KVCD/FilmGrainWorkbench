// Exact fractions, for frame rates (24000/1001) and time bases.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace fgw {

struct Rational {
    std::int64_t num = 0;
    std::int64_t den = 1;

    bool operator==(const Rational&) const = default;
    double value() const { return den == 0 ? 0.0 : static_cast<double>(num) / static_cast<double>(den); }
    bool positive() const { return num > 0 && den > 0; }

    // In lowest terms, the denominator positive.
    Rational reduced() const;
    // The closest fraction whose denominator is at most `max_den` (Python's
    // Fraction.limit_denominator, which the app's frame rates went through).
    Rational limit_denominator(std::int64_t max_den) const;

    std::string to_string() const { return std::to_string(num) + "/" + std::to_string(den); }
    // "24000/1001", "25/1" or "25"; nothing for anything else.
    static std::optional<Rational> parse(std::string_view text);
};

}  // namespace fgw
