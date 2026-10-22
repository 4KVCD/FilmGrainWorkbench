#include "fgw/installation.h"

#include <chrono>
#include <sstream>

#include <windows.h>

extern "C" {
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
}

#include "fgw/fullscan.h"
#include "fgw/text.h"

namespace fgw {

namespace {

constexpr auto check_timeout = std::chrono::seconds(10);  // a program that does not answer by then hangs

struct Answer {
    bool ok = false;
    std::string text;  // its first line, or what went wrong
};

Answer first_line(const std::vector<std::string>& command) {
    platform::ProcessOutput output;
    try {
        output = platform::run_program(command, check_timeout);
    } catch (const platform::ProcessError& error) {
        return {false, std::string("does not run: ") + error.what()};
    }
    if (output.timed_out) {
        return {false, "does not answer within 10 s"};
    }
    const std::string& text = output.standard_output.empty() ? output.standard_error : output.standard_output;
    std::istringstream lines(text);
    std::string line;
    while (std::getline(lines, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (!line.empty()) {
            return {true, line};
        }
    }
    return {false, "no output (exit code " + platform::exit_code_text(output.exit_code) + ")"};
}

Answer ffmpeg_answer(const std::string& ffmpeg) {
    const Answer version = first_line({ffmpeg, "-hide_banner", "-version"});
    if (!version.ok) {
        return version;
    }
    platform::ProcessOutput decoders;
    try {
        decoders = platform::run_program({ffmpeg, "-hide_banner", "-decoders"}, check_timeout);
    } catch (const platform::ProcessError&) {
    }
    std::istringstream lines(decoders.standard_output);
    for (std::string line; std::getline(lines, line);) {
        std::istringstream words(line);
        std::string flags, name;
        if (words >> flags >> name && name == "vvc") {
            return version;
        }
    }
    return {false, version.text + ": no VVC decoder (FFmpeg 7.1 or newer has one)"};
}

ProgramCheck program(const std::string& name, const std::string& purpose, const std::string& path,
                     const std::function<Answer(const std::string&)>& answer) {
    std::error_code error;
    if (path.empty() || !std::filesystem::is_regular_file(utf8_path(path), error)) {
        return {name, purpose, "", "not found", false};
    }
    const Answer found = answer(path);
    return {name, purpose, path, found.text, found.ok};
}

}  // namespace

std::vector<ProgramCheck> check_installation(const Tools& tools, const VvcTools& vvc_tools) {
    const auto version = [](const std::string& path) {
        Answer answer = first_line({path, "--version"});
        // vvdecapp names itself by the path it was started with
        // ("C:\...\bin\vvdecapp.exe version 3.3.0"): the path is shown above it.
        const std::size_t word = answer.text.find(" version ");
        const std::size_t slash = word == std::string::npos ? word : answer.text.find_last_of("\\/", word);
        if (answer.ok && slash != std::string::npos) {
            answer.text.erase(0, slash + 1);
        }
        return answer;
    };
    std::vector<ProgramCheck> checks = {
        program("ffmpeg", "decodes video (both tabs)", tools.ffmpeg, ffmpeg_answer),
        program("ffprobe", "reads video properties (both tabs)", tools.ffprobe,
                [](const std::string& path) { return first_line({path, "-hide_banner", "-version"}); }),
        program("mkvmerge", "writes the MKV (AV1 tab)", tools.mkvmerge, version),
        program("vvencapp", "encodes scenes (VVC tab)", vvc_tools.vvencapp, version),
        program("vvdecapp", "decodes scenes (VVC tab)", vvc_tools.vvdecapp, version),
        program("grain analyzer", "every-frame grain analysis (AV1 tab)", utf8(analyzer_path()),
                [](const std::string& path) {
                    const Answer help = first_line({path, "--help"});
                    return help.ok && help.text.starts_with("usage") ? Answer{true, "runs"} : Answer{false, help.text};
                }),
    };
    // The FFmpeg libraries the app reads encodes with when writing: loaded
    // with the app (it does not start without them), so asked directly which
    // file and version they are (they can be replaced, LGPL).
    std::string library_path;
    if (const HMODULE module = GetModuleHandleW(L"avformat-63.dll")) {
        wchar_t name[MAX_PATH];
        if (const DWORD length = GetModuleFileNameW(module, name, MAX_PATH); length > 0 && length < MAX_PATH) {
            library_path = utf8(std::filesystem::path(std::wstring(name, length)));
        }
    }
    const unsigned libavformat = avformat_version();
    checks.push_back({"FFmpeg libraries", "read the encode when writing (AV1 tab)", library_path,
                      format("libavformat %u.%u.%u (FFmpeg %s)", AV_VERSION_MAJOR(libavformat),
                             AV_VERSION_MINOR(libavformat), AV_VERSION_MICRO(libavformat), av_version_info()),
                      true});
    return checks;
}

std::string format_installation_report(const std::vector<ProgramCheck>& checks, const std::string& app_title) {
    std::string out = app_title + "\nFolder: " + utf8(app_dir()) + "\n\n";
    bool all_ok = true;
    for (const ProgramCheck& c : checks) {
        const std::string status = c.ok ? "OK" : (c.path.empty() ? "MISSING" : "FAIL");
        out += format("%-8s", status.c_str()) + c.name + ": " + c.purpose + "\n";
        out += "        " + (c.path.empty() ? std::string("(not found)") : c.path) + "\n";
        out += "        " + c.detail + "\n";
        all_ok = all_ok && c.ok;
    }
    if (!all_ok) {
        out += "\nffmpeg, ffprobe and mkvmerge are set under Settings > External programs.\n"
               "ffmpeg: ffmpeg.org/download.html (a build with VVC decoding, FFmpeg 7.1 or newer).\n"
               "mkvmerge: mkvtoolnix.download (needed only to write MKVs in the AV1 tab).\n";
    }
    return out;
}

}  // namespace fgw
