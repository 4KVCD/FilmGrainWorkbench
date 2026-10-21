// The reference results the Python app's own functions gave
// (tests/reference/make_reference.py), and the same synthetic inputs.
#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "fgw/grain_params.h"
#include "fgw/picture.h"

namespace fgw::test {

using nlohmann::json;

// tests/reference/<name>, parsed.
json load_reference(const std::string& name);

// synthetic_plane() of make_reference.py.
Plane synthetic_plane(int width, int height, int bit_depth, int salt);
// synthetic_frame() of make_reference.py.
Frame synthetic_frame(int width, int height, int bit_depth, int subx, int suby);

GrainParams params_from_json(const json& j);

// SHA-256 of bytes, as lowercase hex.
std::string sha256(std::span<const std::uint8_t> bytes);
// Of planes as little-endian uint16 samples, one after the other (digest() of make_reference.py).
std::string sha256_planes(std::initializer_list<const Plane*> planes);
// Of int32 values as little-endian bytes.
std::string sha256_ints(const std::vector<int>& values);

std::vector<std::uint8_t> from_hex(const std::string& hex);
std::string to_hex(std::span<const std::uint8_t> bytes);

}  // namespace fgw::test
