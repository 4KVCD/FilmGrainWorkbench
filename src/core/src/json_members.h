// Reading members of parsed JSON without copying them.
//
// nlohmann's value(key, default) returns a copy: iterating over it, or
// keeping pointers into it, reads a temporary that is gone. These return
// references into the document itself.
#pragma once

#include <nlohmann/json.hpp>

namespace fgw {

// `object[key]` if it is an array, else an empty array.
inline const nlohmann::json& array_member(const nlohmann::json& object, const char* key) {
    static const nlohmann::json empty = nlohmann::json::array();
    const auto found = object.find(key);
    return found != object.end() && found->is_array() ? *found : empty;
}

// `object[key]` if it is an object, else an empty object.
inline const nlohmann::json& object_member(const nlohmann::json& object, const char* key) {
    static const nlohmann::json empty = nlohmann::json::object();
    const auto found = object.find(key);
    return found != object.end() && found->is_object() ? *found : empty;
}

}  // namespace fgw
