// Most-significant-bit-first bit writing and reading: the f(n) descriptor of
// the AFGS1 specification and of H.266's headers.
#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <vector>

namespace fgw {

class BitWriter {
public:
    // Appends the `width` low bits of `value`. Throws std::invalid_argument if
    // it does not fit in them.
    void put(std::uint32_t value, int width);
    void put_bits(const BitWriter& other);
    // Appends zero bits up to `total_bits`.
    void pad_to(std::size_t total_bits);

    std::size_t size() const { return bit_count_; }  // in bits
    // The bits, zero-padded at the end to whole bytes.
    const std::vector<std::uint8_t>& bytes() const { return bytes_; }

private:
    void put_bit(bool bit);

    std::vector<std::uint8_t> bytes_;
    std::size_t bit_count_ = 0;
};

class BitReader {
public:
    explicit BitReader(std::span<const std::uint8_t> data) : data_(data) {}

    // The next `width` bits (at most 32). Throws std::out_of_range past the end.
    std::uint32_t get(int width);
    // Exp-Golomb codes of H.266 headers: ue(v) and se(v).
    std::uint32_t ue();
    std::int32_t se();
    // Skips `count` bits. Throws std::out_of_range past the end.
    void skip(std::size_t count);
    // Skips to the next byte boundary.
    void align();

    std::size_t position() const { return position_; }
    std::size_t bits_left() const { return data_.size() * 8 - position_; }

private:
    std::span<const std::uint8_t> data_;
    std::size_t position_ = 0;
};

}  // namespace fgw
