#include "fgw/alignment.h"

#include <algorithm>
#include <cmath>
#include <numeric>

#include "fgw/frames.h"
#include "fgw/temp_folder.h"

namespace fgw {

namespace {

constexpr double match_margin_db = 1.0;  // the paired frame must beat its neighbours by this much
constexpr double match_floor_db = 35.0;  // below this, a tie with the neighbours means nothing matches

double mean(const std::vector<double>& values) {
    return values.empty() ? 0.0 : std::accumulate(values.begin(), values.end(), 0.0) / values.size();
}

double median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const std::size_t n = values.size();
    return n % 2 ? values[n / 2] : (values[n / 2 - 1] + values[n / 2]) / 2;
}

}  // namespace

double psnr(const LumaImage& a, const LumaImage& b, double peak) {
    double squares = 0;
    for (std::size_t i = 0; i < a.samples.size(); ++i) {
        const double d = static_cast<double>(a.samples[i]) - b.samples[i];
        squares += d * d;
    }
    const double mse = squares / static_cast<double>(std::max<std::size_t>(1, a.samples.size()));
    return mse <= 1e-9 ? 99.0 : 10 * std::log10(peak * peak / mse);
}

std::string to_string(Alignment alignment) {
    switch (alignment) {
        case Alignment::ok: return "ok";
        case Alignment::shifted: return "shifted";
        case Alignment::unclear: return "unclear";
    }
    return "unclear";
}

std::pair<double, Alignment> check_alignment(const std::vector<LumaImage>& source,
                                             const std::vector<LumaImage>& encoded, double peak, int max_shift) {
    const int n = static_cast<int>(std::min(source.size(), encoded.size()));
    std::vector<double> matched;
    for (int i = 0; i < n; ++i) {
        matched.push_back(psnr(source[static_cast<std::size_t>(i)], encoded[static_cast<std::size_t>(i)], peak));
    }
    const double matched_mean = mean(matched);
    if (n < 3) {
        return {matched_mean, Alignment::unclear};
    }
    const int reach = std::min(max_shift, n - 2);
    double same = 0;
    double best_shifted = -1e300;
    for (int shift = -reach; shift <= reach; ++shift) {
        std::vector<double> scores;
        for (int i = 0; i < n; ++i) {
            if (i + shift >= 0 && i + shift < n) {
                scores.push_back(psnr(source[static_cast<std::size_t>(i)], encoded[static_cast<std::size_t>(i + shift)], peak));
            }
        }
        const double score = mean(scores);
        if (shift == 0) {
            same = score;
        } else {
            best_shifted = std::max(best_shifted, score);
        }
    }
    if (best_shifted > same + 0.5) {
        return {matched_mean, Alignment::shifted};
    }
    if (same > best_shifted + 0.5) {
        return {matched_mean, Alignment::ok};
    }
    return {matched_mean, Alignment::unclear};  // static content: neighbours look the same
}

DetectedOffset detect_offset(const Tools& tools, const VideoInfo& source, const VideoInfo& encoded,
                             const std::optional<Crop>& crop, CancelToken* cancel, int search, int positions) {
    const int thumb_height = std::max(
        2, static_cast<int>(std::nearbyint(128.0 * encoded.height / encoded.width / 2)) * 2);
    const Geometry thumb{128, thumb_height, 8, 1, 1};
    constexpr int probe_length = 24;
    std::vector<std::pair<std::int64_t, int>> votes;  // in the order found, as the Python app's dict
    std::vector<double> margins;
    const TempFolder folder("afgs1wb-align-");
    for (int k = 0; k < positions; ++k) {
        const auto center = static_cast<std::int64_t>(static_cast<double>(encoded.frame_count()) * (0.25 + 0.25 * k));
        const std::int64_t encoded_start = std::max<std::int64_t>(0, std::min(center, encoded.frame_count() - probe_length));
        const std::int64_t source_start = std::max<std::int64_t>(0, encoded_start - search);
        const std::int64_t source_count = probe_length + 2 * search;
        const auto encoded_path = folder.path() / ("e" + std::to_string(k) + ".yuv");
        const auto source_path = folder.path() / ("s" + std::to_string(k) + ".yuv");
        const std::int64_t ne =
            extract_raw(tools.ffmpeg, encoded, encoded_start, probe_length, encoded_path, cancel, thumb);
        const std::int64_t ns =
            extract_raw(tools.ffmpeg, source, source_start, source_count, source_path, cancel, thumb, crop);
        if (ne < 4 || ns < ne) {
            continue;
        }
        const LumaStack e = luma_planes(encoded_path, thumb, ne);
        const LumaStack s = luma_planes(source_path, thumb, ns);
        // Each shift's score: the mean absolute difference once each frame's
        // mean difference (a brightness change) is taken out.
        std::vector<std::pair<std::int64_t, double>> scores;
        for (std::int64_t shift = 0; shift <= ns - ne; ++shift) {
            double total = 0;
            std::size_t samples = 0;
            for (std::int64_t f = 0; f < ne; ++f) {
                const auto& sp = s.planes[static_cast<std::size_t>(shift + f)];
                const auto& ep = e.planes[static_cast<std::size_t>(f)];
                double frame_mean = 0;
                for (std::size_t i = 0; i < ep.size(); ++i) {
                    frame_mean += static_cast<double>(sp[i]) - ep[i];
                }
                frame_mean /= static_cast<double>(ep.size());
                for (std::size_t i = 0; i < ep.size(); ++i) {
                    total += std::abs(static_cast<double>(sp[i]) - ep[i] - frame_mean);
                }
                samples += ep.size();
            }
            scores.emplace_back(source_start + shift - encoded_start, total / static_cast<double>(samples));
        }
        std::stable_sort(scores.begin(), scores.end(), [](const auto& a, const auto& b) { return a.second < b.second; });
        const std::int64_t best = scores.front().first;
        const auto vote = std::find_if(votes.begin(), votes.end(), [best](const auto& v) { return v.first == best; });
        if (vote == votes.end()) {
            votes.emplace_back(best, 1);
        } else {
            ++vote->second;
        }
        if (scores.size() > 1) {
            margins.push_back(scores[1].second / std::max(scores[0].second, 1e-6));
        }
    }
    if (votes.empty()) {
        throw std::runtime_error("could not decode enough frames to detect the offset");
    }
    // The most votes; of equal counts, the first found.
    const auto winner = std::max_element(votes.begin(), votes.end(),
                                         [](const auto& a, const auto& b) { return a.second < b.second; });
    const double spread = margins.empty() ? 0.0 : std::min(1.0, median(margins) - 1.0);
    const double confidence = static_cast<double>(winner->second) / positions * spread;
    return {winner->first, std::clamp(confidence, 0.0, 1.0)};
}

LumaImage block_means(const Plane& luma, int size) {
    LumaImage means;
    means.width = luma.width / size;
    means.height = luma.height / size;
    means.samples.assign(static_cast<std::size_t>(means.width) * means.height, 0.0f);
    const double area = static_cast<double>(size) * size;
    for (int by = 0; by < means.height; ++by) {
        std::vector<double> sums(static_cast<std::size_t>(means.width), 0.0);
        for (int y = by * size; y < (by + 1) * size; ++y) {
            const std::uint16_t* row = luma.row(y);
            for (int bx = 0; bx < means.width; ++bx) {
                for (int x = bx * size; x < (bx + 1) * size; ++x) {
                    sums[static_cast<std::size_t>(bx)] += row[x];
                }
            }
        }
        for (int bx = 0; bx < means.width; ++bx) {
            means.samples[static_cast<std::size_t>(by) * means.width + bx] =
                static_cast<float>(sums[static_cast<std::size_t>(bx)] / area);
        }
    }
    return means;
}

FrameMatch frame_match(const std::map<std::int64_t, LumaImage>& source, const LumaImage& encoded,
                       std::int64_t paired, double peak) {
    const auto found = source.find(paired);
    if (found == source.end()) {
        return {MatchVerdict::missing, std::nullopt, std::nullopt};
    }
    const double same = psnr(found->second, encoded, peak);
    std::optional<double> best;
    for (const std::int64_t neighbour : {paired - 1, paired + 1}) {
        if (const auto other = source.find(neighbour); other != source.end()) {
            const double value = psnr(other->second, encoded, peak);
            best = best ? std::max(*best, value) : value;
        }
    }
    if (!best) {
        return {MatchVerdict::unclear, same, std::nullopt};
    }
    if (*best > same + match_margin_db) {
        return {MatchVerdict::neighbour, same, best};
    }
    if (same > *best + match_margin_db) {
        return {MatchVerdict::same, same, best};
    }
    return {same < match_floor_db ? MatchVerdict::none : MatchVerdict::unclear, same, best};
}

}  // namespace fgw
