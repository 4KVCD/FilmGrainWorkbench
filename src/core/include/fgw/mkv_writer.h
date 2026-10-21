// A minimal Matroska writer for one VVC video track.
//
// It stores the packets exactly as given (length-prefixed NAL units), the
// original vvcC codec private data and each packet's own presentation time
// in milliseconds, so mkvmerge can then merge it with the other streams
// losslessly. (ffmpeg -c copy loses VVC's timestamps from Matroska.)
#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <span>
#include <vector>

namespace fgw {

class MatroskaVideoWriter {
public:
    // Starts the file. `frame_duration_ns` (0: none) becomes the track's
    // default duration. Throws std::runtime_error if it cannot be written.
    MatroskaVideoWriter(std::filesystem::path path, std::span<const std::uint8_t> codec_private, int width,
                        int height, std::int64_t frame_duration_ns = 0);
    ~MatroskaVideoWriter();  // an unfinished file is removed (abort)

    MatroskaVideoWriter(const MatroskaVideoWriter&) = delete;
    MatroskaVideoWriter& operator=(const MatroskaVideoWriter&) = delete;

    void write(std::span<const std::uint8_t> data, std::int64_t pts_ms, bool keyframe, std::int64_t duration_ms = 0);
    // Writes what is left and the sizes; the file is complete.
    void close();
    // Stops and removes the file.
    void abort();

    std::int64_t blocks() const { return blocks_; }

private:
    void flush_cluster();

    std::filesystem::path path_;
    std::ofstream file_;
    std::streamoff segment_size_at_ = 0;
    std::streamoff segment_start_ = 0;
    std::streamoff duration_at_ = 0;
    std::int64_t cluster_ts_ = -1;  // -1: no cluster open
    std::vector<std::uint8_t> cluster_;
    std::int64_t last_end_ms_ = 0;
    std::int64_t blocks_ = 0;
    bool closed_ = false;
};

}  // namespace fgw
