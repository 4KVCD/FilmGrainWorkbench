#include "fgw/text.h"

#include <windows.h>
#include <bcrypt.h>

#include <charconv>
#include <chrono>
#include <stdexcept>
#include <cstdarg>
#include <cstdio>

namespace fgw {

std::filesystem::path utf8_path(std::string_view text) {
    return std::filesystem::path(std::u8string(text.begin(), text.end()));
}

std::string utf8(const std::filesystem::path& path) {
    const std::u8string text = path.u8string();
    return std::string(text.begin(), text.end());
}

std::string with_thousands(std::int64_t value) {
    std::string digits = std::to_string(value < 0 ? -value : value);
    for (std::ptrdiff_t at = static_cast<std::ptrdiff_t>(digits.size()) - 3; at > 0; at -= 3) {
        digits.insert(static_cast<std::size_t>(at), 1, ',');
    }
    return value < 0 ? "-" + digits : digits;
}

std::string python_float(double value) {
    char buffer[64];
    const auto [end, error] = std::to_chars(buffer, buffer + sizeof buffer, value);
    std::string text(buffer, error == std::errc() ? end : buffer);
    if (text.find_first_of(".eEn") == std::string::npos) {  // n: inf, nan
        text += ".0";
    }
    return text;
}

std::string sha1_hex(std::string_view text) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    std::uint8_t digest[20] = {};
    const bool done = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA1_ALGORITHM, nullptr, 0) == 0 &&
                      BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) == 0 &&
                      BCryptHashData(hash, reinterpret_cast<PUCHAR>(const_cast<char*>(text.data())),
                                     static_cast<ULONG>(text.size()), 0) == 0 &&
                      BCryptFinishHash(hash, digest, sizeof digest, 0) == 0;
    if (hash != nullptr) {
        BCryptDestroyHash(hash);
    }
    if (algorithm != nullptr) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
    }
    if (!done) {
        throw std::runtime_error("SHA-1 is not available");
    }
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (const std::uint8_t byte : digest) {
        out += digits[byte >> 4];
        out += digits[byte & 15];
    }
    return out;
}

FileStamp file_stamp(const std::filesystem::path& path) {
    const auto changed = std::chrono::clock_cast<std::chrono::system_clock>(std::filesystem::last_write_time(path));
    const auto seconds = std::chrono::floor<std::chrono::seconds>(changed.time_since_epoch()).count();
    return {std::filesystem::file_size(path), static_cast<std::int64_t>(seconds)};
}

std::string format(const char* pattern, ...) {
    va_list arguments;
    va_start(arguments, pattern);
    va_list copy;
    va_copy(copy, arguments);
    const int length = std::vsnprintf(nullptr, 0, pattern, copy);
    va_end(copy);
    std::string out(static_cast<std::size_t>(length > 0 ? length : 0), '\0');
    if (length > 0) {
        std::vsnprintf(out.data(), out.size() + 1, pattern, arguments);
    }
    va_end(arguments);
    return out;
}

}  // namespace fgw
