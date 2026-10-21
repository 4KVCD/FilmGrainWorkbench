// The external programs (ffmpeg, ffprobe, mkvmerge, and the bundled
// vvencapp, vvdecapp and grain analyzer), where they are, and running them
// so that a job can be cancelled.
#pragma once

#include <atomic>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "fgw/platform/process.h"

namespace fgw {

// The user cancelled the job.
class Cancelled : public std::runtime_error {
public:
    Cancelled() : std::runtime_error("cancelled") {}
};

// A program failed, or is missing; the message says which and why.
class ToolError : public std::runtime_error {
public:
    explicit ToolError(const std::string& message, int exit_code = -1)
        : std::runtime_error(message), exit_code_(exit_code) {}
    // The program's exit code; -1 when it did not run or was not the cause.
    int exit_code() const { return exit_code_; }

private:
    int exit_code_;
};

// Shared between a job and the window: cancelling ends the programs the job
// is running. Thread-safe.
class CancelToken {
public:
    void cancel();
    bool cancelled() const { return cancelled_.load(); }
    // Throws Cancelled once cancelled.
    void check() const;

    // While it lives, cancelling ends `process` (at once if already cancelled).
    class Tracking {
    public:
        Tracking(CancelToken* token, platform::ChildProcess& process);
        ~Tracking();
        Tracking(const Tracking&) = delete;
        Tracking& operator=(const Tracking&) = delete;

    private:
        CancelToken* token_;
        platform::ChildProcess* process_;
    };

private:
    std::atomic<bool> cancelled_ = false;
    std::mutex mutex_;
    std::set<platform::ChildProcess*> processes_;
};

// How long-running work reports: `fraction` 0..1, or negative when unknown.
using Progress = std::function<void(double fraction, const std::string& message)>;

// The folder the app's bin/ and licenses/ are in: the executable's.
std::filesystem::path app_dir();

// A program the app ships in its bin folder (afgs1_grain_analyzer,
// vvencapp, vvdecapp): next to the executable, else in the source tree's
// bin folder for a build that is not packaged. Empty if neither has it.
std::filesystem::path bundled_program(const std::string& name);

// What to do when a bundled program is missing.
std::string missing_program_hint();

// A program on the PATH (with PATHEXT's extensions); empty if not found.
std::filesystem::path find_on_path(const std::string& name);

struct Tools {
    std::string ffmpeg;
    std::string ffprobe;
    std::string mkvmerge;

    // The labels of the programs named (all three when none) that are not there.
    std::vector<std::string> missing(const std::vector<std::string>& names = {}) const;
    static const std::vector<std::string>& names();  // ffmpeg, ffprobe, mkvmerge
    static std::string label(const std::string& name);  // "mkvmerge (MKVToolNix)"
    std::string& path(const std::string& name);
    const std::string& path(const std::string& name) const;
};

// Best effort: the app's bin folder, the PATH, MKVToolNix's install folder.
Tools detect_tools();

struct ToolOutput {
    std::string standard_output;  // bytes as written
    std::string error_log;        // standard error's end, lines ended with "\n"
};

struct RunOptions {
    // Called with each line of standard error as it comes (on another thread).
    std::function<void(std::string_view)> on_error_line;
    // Written to standard input, then closed.
    std::shared_ptr<const std::string> input_data;
    // At below-normal priority (platform::ProcessOptions::background).
    bool background = false;
};

// The error a program that failed with `exit_code` is reported with:
// "<name> failed (exit code N):" and the last 12 lines of its error log
// (without showinfo's frame lines).
ToolError tool_failure(const std::string& program, int exit_code, const std::string& error_log);

// Runs a program to its end. Throws ToolError ("<name> failed (exit code N):"
// and the last lines of its error output) if it fails or cannot be started,
// Cancelled if the token is cancelled meanwhile.
ToolOutput run_tool(const std::vector<std::string>& command, CancelToken* cancel = nullptr,
                    const RunOptions& options = {});

int cpu_count();

// Keeps Windows from sleeping while long work runs (the display may still
// turn off). Per thread: switch it off on the thread that switched it on.
void keep_awake(bool on);

}  // namespace fgw
