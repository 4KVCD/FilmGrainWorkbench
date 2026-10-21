#include "vvc_scene.h"

namespace fgw::app {

bool Scene::covers(std::int64_t index) const {
    const std::int64_t first = cut ? cut->start : start;
    const std::int64_t count = cut ? cut->count : frames;
    return first <= index && index < first + count;
}

bool Scene::holds(std::int64_t index, std::int64_t length, const std::optional<Crop>& wanted_crop) const {
    return covers(index) && frames == length && crop == wanted_crop;
}

VvcSettings Scene::settings(int fga) const { return VvcSettings{fga, base, static_cast<int>(frames)}; }

std::optional<JobKey> Scene::next_work(int selected_fga) const {
    if (!cut) {
        return JobKey{JobKey::Kind::cut, ident, {}, 0};
    }
    if (!clips.contains(selected_fga)) {
        return JobKey{JobKey::Kind::encode, ident, base, selected_fga};
    }
    for (const FgaMode& mode : fga_modes) {
        if (!clips.contains(mode.value)) {
            return JobKey{JobKey::Kind::encode, ident, base, mode.value};
        }
    }
    return std::nullopt;
}

bool Scene::wants(const JobKey& key) const {
    if (key.scene != ident) {
        return false;
    }
    if (key.kind == JobKey::Kind::cut) {
        return !cut.has_value();
    }
    return key.coding == base && !clips.contains(key.fga);
}

}  // namespace fgw::app
