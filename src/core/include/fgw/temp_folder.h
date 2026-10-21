// A temporary folder, deleted with everything in it when it goes.
#pragma once

#include <filesystem>
#include <string>

namespace fgw {

class TempFolder {
public:
    // A new folder "<prefix><random>" in `parent` (the system's temporary
    // folder when empty). Throws std::filesystem::filesystem_error.
    explicit TempFolder(const std::string& prefix, const std::filesystem::path& parent = {});
    ~TempFolder();
    TempFolder(const TempFolder&) = delete;
    TempFolder& operator=(const TempFolder&) = delete;

    const std::filesystem::path& path() const { return path_; }

private:
    std::filesystem::path path_;
};

// A new folder "<prefix><random>" in `parent`, kept (the caller removes it).
std::filesystem::path make_unique_folder(const std::string& prefix, const std::filesystem::path& parent);

}  // namespace fgw
