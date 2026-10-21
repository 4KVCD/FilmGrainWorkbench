#include "fgw/bits.h"

#include <string>

namespace fgw {

void BitWriter::put_bit(bool bit) {
    if (bit_count_ % 8 == 0) {
        bytes_.push_back(0);
    }
    if (bit) {
        bytes_.back() |= static_cast<std::uint8_t>(0x80u >> (bit_count_ % 8));
    }
    ++bit_count_;
}

void BitWriter::put(std::uint32_t value, int width) {
    if (width < 0 || width > 32) {
        throw std::invalid_argument("bit width " + std::to_string(width) + " is not 0..32");
    }
    if (width < 32 && value >> width != 0) {
        throw std::invalid_argument(std::to_string(value) + " does not fit in " + std::to_string(width) + " bits");
    }
    for (int bit = width - 1; bit >= 0; --bit) {
        put_bit((value >> bit) & 1u);
    }
}

void BitWriter::put_bits(const BitWriter& other) {
    for (std::size_t i = 0; i < other.bit_count_; ++i) {
        put_bit((other.bytes_[i / 8] >> (7 - i % 8)) & 1u);
    }
}

void BitWriter::pad_to(std::size_t total_bits) {
    if (total_bits < bit_count_) {
        throw std::invalid_argument("cannot pad to fewer bits than already written");
    }
    while (bit_count_ < total_bits) {
        put_bit(false);
    }
}

std::uint32_t BitReader::get(int width) {
    if (width < 0 || width > 32 || static_cast<std::size_t>(width) > bits_left()) {
        throw std::out_of_range("read past the end of the data");
    }
    std::uint32_t value = 0;
    for (int i = 0; i < width; ++i) {
        const std::uint8_t byte = data_[position_ >> 3];
        value = (value << 1) | ((byte >> (7 - (position_ & 7))) & 1u);
        ++position_;
    }
    return value;
}

std::uint32_t BitReader::ue() {
    int zeros = 0;
    while (get(1) == 0) {
        if (++zeros > 31) {
            throw std::out_of_range("bad Exp-Golomb code");
        }
    }
    return static_cast<std::uint32_t>((std::uint64_t{1} << zeros) - 1 + get(zeros));
}

std::int32_t BitReader::se() {
    const std::uint32_t k = ue();
    const auto half = static_cast<std::int32_t>((k + 1) / 2);
    return (k & 1u) ? half : -static_cast<std::int32_t>(k / 2);
}

void BitReader::skip(std::size_t count) {
    if (count > bits_left()) {
        throw std::out_of_range("read past the end of the data");
    }
    position_ += count;
}

void BitReader::align() {
    skip((8 - position_ % 8) % 8);
}

}  // namespace fgw
