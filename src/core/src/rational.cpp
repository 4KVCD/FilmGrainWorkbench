#include "fgw/rational.h"

#include <charconv>
#include <cstdlib>
#include <numeric>

namespace fgw {

Rational Rational::reduced() const {
    if (den == 0) {
        return *this;
    }
    const std::int64_t divisor = std::gcd(num, den);
    Rational r{num / divisor, den / divisor};
    if (r.den < 0) {
        r = {-r.num, -r.den};
    }
    return r;
}

Rational Rational::limit_denominator(std::int64_t max_den) const {
    // Python's algorithm (fractions.Fraction.limit_denominator): the best
    // approximations from the continued fraction, then the closer of the
    // last convergent and the semiconvergent below the limit.
    const Rational self = reduced();
    if (self.den <= max_den) {
        return self;
    }
    std::int64_t p0 = 0, q0 = 1, p1 = 1, q1 = 0;
    std::int64_t n = self.num, d = self.den;
    while (true) {
        const std::int64_t a = n / d;
        const std::int64_t q2 = q0 + a * q1;
        if (q2 > max_den) {
            break;
        }
        const std::int64_t p_next = p0 + a * p1;
        p0 = p1;
        q0 = q1;
        p1 = p_next;
        q1 = q2;
        const std::int64_t remainder = n - a * d;
        n = d;
        d = remainder;
    }
    const std::int64_t k = (max_den - q0) / q1;
    // Which of (p0 + k p1) / (q0 + k q1) and p1 / q1 is closer, exactly.
    if (2 * d * (q0 + k * q1) <= self.den) {
        return Rational{p1, q1}.reduced();
    }
    return Rational{p0 + k * p1, q0 + k * q1}.reduced();
}

std::optional<Rational> Rational::parse(std::string_view text) {
    const auto read = [](std::string_view part) -> std::optional<std::int64_t> {
        std::int64_t value = 0;
        const auto [end, error] = std::from_chars(part.data(), part.data() + part.size(), value);
        if (error != std::errc() || end != part.data() + part.size()) {
            return std::nullopt;
        }
        return value;
    };
    const std::size_t slash = text.find('/');
    const std::optional<std::int64_t> num = read(text.substr(0, slash));
    const std::optional<std::int64_t> den =
        slash == std::string_view::npos ? std::optional<std::int64_t>(1) : read(text.substr(slash + 1));
    if (!num || !den) {
        return std::nullopt;
    }
    return Rational{*num, *den};
}

}  // namespace fgw
