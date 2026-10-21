// Text the app shows and passes on: UTF-8 strings for paths, and numbers
// formatted as the Python app formatted them.
#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>

namespace fgw {

// A path from UTF-8 text, and back.
std::filesystem::path utf8_path(std::string_view text);
std::string utf8(const std::filesystem::path& path);

// 150000 -> "150,000" (Python's "{:,}").
std::string with_thousands(std::int64_t value);

// A double as Python's repr() writes it: the shortest text that reads back
// the same, with ".0" on whole numbers ("1000.0", "0.1", "1e+16").
std::string python_float(double value);

// SHA-1 of `text` as lowercase hex (the names of the app's saved analyses).
std::string sha1_hex(std::string_view text);

// A file's size and last change as Python's os.stat gave them: bytes, and
// whole seconds since 1970 (int(st_mtime)). Throws std::filesystem::filesystem_error.
struct FileStamp {
    std::uintmax_t size = 0;
    std::int64_t mtime = 0;
};
FileStamp file_stamp(const std::filesystem::path& path);

// printf-style formatting into a std::string.
std::string format(const char* pattern, ...);

}  // namespace fgw
