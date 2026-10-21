#include "reference.h"

#include <windows.h>
#include <bcrypt.h>

#include <fstream>
#include <sstream>
#include <stdexcept>

namespace fgw::test {

json load_reference(const std::string& name) {
    std::ifstream file(std::string(FGW_REFERENCE_DIR) + "/" + name);
    if (!file) {
        throw std::runtime_error("no reference file " + name);
    }
    return json::parse(file);
}

Plane synthetic_plane(int width, int height, int bit_depth, int salt) {
    const std::int64_t modulus = std::int64_t{1} << bit_depth;
    Plane plane(width, height);
    for (std::int64_t r = 0; r < height; ++r) {
        for (std::int64_t c = 0; c < width; ++c) {
            const std::int64_t value = (r * 37 + c * 11 + (r * c + salt) % 13 * salt + salt * 101) % modulus;
            plane.row(static_cast<int>(r))[c] = static_cast<std::uint16_t>(value);
        }
    }
    return plane;
}

Frame synthetic_frame(int width, int height, int bit_depth, int subx, int suby) {
    Frame frame;
    frame.geometry = {width, height, bit_depth, subx, suby};
    frame.y = synthetic_plane(width, height, bit_depth, 1);
    const int cw = (width + subx) >> subx;
    const int ch = (height + suby) >> suby;
    frame.u = synthetic_plane(cw, ch, bit_depth, 2);
    frame.v = synthetic_plane(cw, ch, bit_depth, 3);
    return frame;
}

namespace {

std::vector<ScalingPoint> points_from_json(const json& j) {
    std::vector<ScalingPoint> points;
    for (const json& point : j) {
        points.push_back({point.at(0).get<int>(), point.at(1).get<int>()});
    }
    return points;
}

}  // namespace

GrainParams params_from_json(const json& j) {
    GrainParams p;
    p.apply_grain = j.at("apply_grain").get<bool>();
    p.random_seed = j.at("random_seed").get<int>();
    p.y_points = points_from_json(j.at("y_points"));
    p.cb_points = points_from_json(j.at("cb_points"));
    p.cr_points = points_from_json(j.at("cr_points"));
    p.chroma_scaling_from_luma = j.at("chroma_scaling_from_luma").get<bool>();
    p.scaling_shift = j.at("scaling_shift").get<int>();
    p.ar_coeff_lag = j.at("ar_coeff_lag").get<int>();
    p.ar_coeffs_y = j.at("ar_coeffs_y").get<std::vector<int>>();
    p.ar_coeffs_cb = j.at("ar_coeffs_cb").get<std::vector<int>>();
    p.ar_coeffs_cr = j.at("ar_coeffs_cr").get<std::vector<int>>();
    p.ar_coeff_shift = j.at("ar_coeff_shift").get<int>();
    p.grain_scale_shift = j.at("grain_scale_shift").get<int>();
    p.cb_mult = j.at("cb_mult").get<int>();
    p.cb_luma_mult = j.at("cb_luma_mult").get<int>();
    p.cb_offset = j.at("cb_offset").get<int>();
    p.cr_mult = j.at("cr_mult").get<int>();
    p.cr_luma_mult = j.at("cr_luma_mult").get<int>();
    p.cr_offset = j.at("cr_offset").get<int>();
    p.overlap_flag = j.at("overlap_flag").get<bool>();
    p.clip_to_restricted_range = j.at("clip_to_restricted_range").get<bool>();
    return p;
}

namespace {

// SHA-256 with Windows' CNG, fed piece by piece.
class Sha256 {
public:
    Sha256() {
        if (BCryptOpenAlgorithmProvider(&algorithm_, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0 ||
            BCryptCreateHash(algorithm_, &hash_, nullptr, 0, nullptr, 0, 0) != 0) {
            throw std::runtime_error("SHA-256 is not available");
        }
    }
    ~Sha256() {
        BCryptDestroyHash(hash_);
        BCryptCloseAlgorithmProvider(algorithm_, 0);
    }
    void add(const void* data, std::size_t bytes) {
        BCryptHashData(hash_, static_cast<PUCHAR>(const_cast<void*>(data)), static_cast<ULONG>(bytes), 0);
    }
    std::string hex() {
        std::uint8_t digest[32];
        BCryptFinishHash(hash_, digest, sizeof digest, 0);
        return to_hex(digest);
    }

private:
    BCRYPT_ALG_HANDLE algorithm_ = nullptr;
    BCRYPT_HASH_HANDLE hash_ = nullptr;
};

}  // namespace

std::string sha256(std::span<const std::uint8_t> bytes) {
    Sha256 hash;
    hash.add(bytes.data(), bytes.size());
    return hash.hex();
}

std::string sha256_planes(std::initializer_list<const Plane*> planes) {
    Sha256 hash;
    for (const Plane* plane : planes) {
        hash.add(plane->samples.data(), plane->bytes());
    }
    return hash.hex();
}

std::string sha256_ints(const std::vector<int>& values) {
    Sha256 hash;
    hash.add(values.data(), values.size() * sizeof(int));
    return hash.hex();
}

std::vector<std::uint8_t> from_hex(const std::string& hex) {
    std::vector<std::uint8_t> bytes;
    for (std::size_t i = 0; i + 1 < hex.size(); i += 2) {
        bytes.push_back(static_cast<std::uint8_t>(std::stoi(hex.substr(i, 2), nullptr, 16)));
    }
    return bytes;
}

std::string to_hex(std::span<const std::uint8_t> bytes) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (const std::uint8_t b : bytes) {
        out += digits[b >> 4];
        out += digits[b & 15];
    }
    return out;
}

}  // namespace fgw::test
