#include "fgw/temp_folder.h"

#include <random>

namespace fgw {

std::filesystem::path make_unique_folder(const std::string& prefix, const std::filesystem::path& parent) {
    static constexpr char letters[] = "abcdefghijklmnopqrstuvwxyz0123456789";
    std::random_device seed;
    std::mt19937 random(seed());
    std::uniform_int_distribution<int> pick(0, static_cast<int>(sizeof letters) - 2);
    for (int attempt = 0; attempt < 100; ++attempt) {
        std::string name = prefix;
        for (int i = 0; i < 8; ++i) {
            name += letters[pick(random)];
        }
        const std::filesystem::path candidate = parent / name;
        if (std::filesystem::create_directory(candidate)) {  // false: it exists already
            return candidate;
        }
    }
    throw std::filesystem::filesystem_error("no unique folder name found", parent,
                                            std::make_error_code(std::errc::file_exists));
}

TempFolder::TempFolder(const std::string& prefix, const std::filesystem::path& parent)
    : path_(make_unique_folder(prefix, parent.empty() ? std::filesystem::temp_directory_path() : parent)) {}

TempFolder::~TempFolder() {
    std::error_code ignored;
    std::filesystem::remove_all(path_, ignored);
}

}  // namespace fgw
