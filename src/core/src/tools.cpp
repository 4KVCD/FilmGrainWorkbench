#include "fgw/tools.h"

#include <windows.h>

#include <algorithm>
#include <sstream>
#include <thread>

#include "fgw/text.h"

namespace fgw {

void CancelToken::cancel() {
    cancelled_ = true;
    const std::lock_guard lock(mutex_);
    for (platform::ChildProcess* process : processes_) {
        process->terminate();
    }
}

void CancelToken::check() const {
    if (cancelled_) {
        throw Cancelled();
    }
}

CancelToken::Tracking::Tracking(CancelToken* token, platform::ChildProcess& process)
    : token_(token), process_(&process) {
    if (token_ == nullptr) {
        return;
    }
    const std::lock_guard lock(token_->mutex_);
    token_->processes_.insert(process_);
    if (token_->cancelled_) {
        process_->terminate();
    }
}

CancelToken::Tracking::~Tracking() {
    if (token_ != nullptr) {
        const std::lock_guard lock(token_->mutex_);
        token_->processes_.erase(process_);
    }
}

std::filesystem::path app_dir() {
    std::wstring buffer(MAX_PATH, L'\0');
    while (true) {
        const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
        if (length < buffer.size()) {
            buffer.resize(length);
            break;
        }
        buffer.resize(buffer.size() * 2);
    }
    return std::filesystem::path(buffer).parent_path();
}

namespace {

// The source tree's bin folder, where build.py puts the programs: what a
// build that is not packaged runs.
std::filesystem::path source_bin() {
#ifdef FGW_SOURCE_DIR
    return (std::filesystem::path(FGW_SOURCE_DIR) / "bin").make_preferred();  // CMake writes "/"
#else
    return {};
#endif
}

bool packaged() { return std::filesystem::is_directory(app_dir() / "bin"); }

}  // namespace

std::filesystem::path bundled_program(const std::string& name) {
    for (const std::filesystem::path& folder : {app_dir() / "bin", source_bin()}) {
        if (folder.empty()) {
            continue;
        }
        const std::filesystem::path candidate = folder / (name + ".exe");
        std::error_code error;
        if (std::filesystem::is_regular_file(candidate, error)) {
            return candidate;
        }
    }
    return {};
}

std::string missing_program_hint() {
    return packaged() ? "The app's bin folder is incomplete: download and unzip the app again."
                      : "Build it with: python build.py";
}

std::filesystem::path find_on_path(const std::string& name) {
    const std::wstring wide(name.begin(), name.end());
    wchar_t found[MAX_PATH];
    const DWORD length = SearchPathW(nullptr, wide.c_str(), L".exe", MAX_PATH, found, nullptr);
    if (length == 0 || length >= MAX_PATH) {
        return {};
    }
    return std::filesystem::path(std::wstring(found, length));
}

const std::vector<std::string>& Tools::names() {
    static const std::vector<std::string> all = {"ffmpeg", "ffprobe", "mkvmerge"};
    return all;
}

std::string Tools::label(const std::string& name) { return name == "mkvmerge" ? "mkvmerge (MKVToolNix)" : name; }

std::string& Tools::path(const std::string& name) {
    return name == "ffmpeg" ? ffmpeg : name == "ffprobe" ? ffprobe : mkvmerge;
}

const std::string& Tools::path(const std::string& name) const { return const_cast<Tools*>(this)->path(name); }

std::vector<std::string> Tools::missing(const std::vector<std::string>& wanted) const {
    std::vector<std::string> out;
    for (const std::string& name : wanted.empty() ? names() : wanted) {
        std::error_code error;
        const std::string& where = path(name);
        if (where.empty() || !std::filesystem::is_regular_file(utf8_path(where), error)) {
            out.push_back(label(name));
        }
    }
    return out;
}

Tools detect_tools() {
    const auto find = [](const std::string& name) -> std::string {
        std::filesystem::path where = bundled_program(name);
        if (where.empty()) {
            where = find_on_path(name);
        }
        return where.empty() ? std::string() : utf8(where);
    };
    Tools tools{find("ffmpeg"), find("ffprobe"), find("mkvmerge")};
    if (tools.mkvmerge.empty()) {
        for (const wchar_t* variable : {L"ProgramFiles", L"ProgramFiles(x86)"}) {
            wchar_t root[MAX_PATH];
            const DWORD length = GetEnvironmentVariableW(variable, root, MAX_PATH);
            if (length == 0 || length >= MAX_PATH) {
                continue;
            }
            const std::filesystem::path candidate =
                std::filesystem::path(std::wstring(root, length)) / "MKVToolNix" / "mkvmerge.exe";
            std::error_code error;
            if (std::filesystem::is_regular_file(candidate, error)) {
                tools.mkvmerge = utf8(candidate);
                break;
            }
        }
    }
    return tools;
}

namespace {

// The program's file name, for messages.
std::string program_name(const std::string& path) {
    return utf8(utf8_path(path).filename());
}

// The last 12 lines of an error log, without showinfo's frame lines.
std::string error_lines(const std::string& log) {
    std::vector<std::string> lines;
    std::istringstream stream(log);
    for (std::string line; std::getline(stream, line);) {
        if (!line.empty() && line.find("Parsed_showinfo") == std::string::npos) {
            lines.push_back(line);
        }
    }
    const std::size_t first = lines.size() > 12 ? lines.size() - 12 : 0;
    std::string out;
    for (std::size_t i = first; i < lines.size(); ++i) {
        out += (out.empty() ? "" : "\n") + lines[i];
    }
    return out;
}

}  // namespace

ToolError tool_failure(const std::string& program, int exit_code, const std::string& error_log) {
    return ToolError(program_name(program) + " failed (exit code " + std::to_string(exit_code) + "):\n" +
                         error_lines(error_log),
                     exit_code);
}

ToolOutput run_tool(const std::vector<std::string>& command, CancelToken* cancel, const RunOptions& options) {
    if (cancel != nullptr) {
        cancel->check();
    }
    platform::ProcessOptions process_options;
    process_options.on_error_line = options.on_error_line;
    process_options.input_data = options.input_data;
    process_options.background = options.background;
    platform::ChildProcess child = [&] {
        try {
            return platform::ChildProcess::start(command, process_options);
        } catch (const platform::ProcessError& error) {
            throw ToolError("Program not found: " + command.front() + " (" + error.what() + ")");
        }
    }();
    const CancelToken::Tracking tracking(cancel, child);
    ToolOutput output;
    char buffer[64 * 1024];
    while (const std::size_t count = child.read(buffer, sizeof buffer)) {
        output.standard_output.append(buffer, count);
    }
    const int exit_code = child.wait();
    output.error_log = child.standard_error();
    if (cancel != nullptr && cancel->cancelled()) {
        throw Cancelled();
    }
    if (exit_code != 0) {
        throw ToolError(program_name(command.front()) + " failed (exit code " + std::to_string(exit_code) +
                        "):\n" + error_lines(output.error_log));
    }
    return output;
}

int cpu_count() { return std::max(1u, std::thread::hardware_concurrency()); }

void keep_awake(bool on) {
    SetThreadExecutionState(ES_CONTINUOUS | (on ? ES_SYSTEM_REQUIRED : 0));
}

}  // namespace fgw
