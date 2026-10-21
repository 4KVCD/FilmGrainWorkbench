// The VVC tab's scene: the frames "Encode this scene" was pressed for, cut
// once from the source and encoded in each analysis mode, and which piece of
// work it needs next.
#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <memory>
#include <optional>

#include "fgw/vvc_grain.h"

namespace fgw::app {

// What the tab's one background job does.
struct JobKey {
    enum class Kind { find_black_bars, cut, encode };
    Kind kind = Kind::find_black_bars;
    int scene = 0;      // Scene::ident
    VvcCoding coding;   // of an encode
    int fga = 0;        // the mode an encode is for
};

struct Scene {
    int ident = 0;
    std::int64_t start = 0;
    std::int64_t frames = 0;
    std::optional<Crop> crop;
    VvcCoding base;  // the coding the encodes use
    std::filesystem::path folder;
    std::optional<SceneCut> cut;
    std::map<int, std::shared_ptr<const Clip>> clips;  // by --fga, encoded with `base`
    std::map<int, std::shared_ptr<const Clip>> older;  // earlier codings, shown until replaced

    bool covers(std::int64_t index) const;
    // Whether Encode this scene at `index` would cut these same frames again.
    bool holds(std::int64_t index, std::int64_t length, const std::optional<Crop>& wanted_crop) const;
    VvcSettings settings(int fga) const;
    // The cut first, then the selected mode, then the others in order;
    // nothing when all is done.
    std::optional<JobKey> next_work(int selected_fga) const;
    // Whether the result of the job `key` still belongs in this scene.
    bool wants(const JobKey& key) const;
};

}  // namespace fgw::app
