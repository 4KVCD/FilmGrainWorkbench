// Which programs the app found, and whether they answer: Help > Check
// installation, and --check-installation FILE.
#pragma once

#include <string>
#include <vector>

#include "fgw/tools.h"
#include "fgw/vvc_grain.h"

namespace fgw {

struct ProgramCheck {
    std::string name;
    std::string purpose;
    std::string path;    // empty when not found
    std::string detail;  // its version line, or what is wrong
    bool ok = false;
};

// Runs each program for its version (up to 10 s each). Takes seconds: call
// it off the window's thread.
std::vector<ProgramCheck> check_installation(const Tools& tools, const VvcTools& vvc_tools);

// The report as the dialog shows it and --check-installation writes it.
std::string format_installation_report(const std::vector<ProgramCheck>& checks, const std::string& app_title);

}  // namespace fgw
