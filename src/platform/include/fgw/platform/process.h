// Starting programs (ffmpeg, ffprobe, vvencapp, vvdecapp, the grain
// analyzer, mkvmerge) and reading what they write. From VideoMetricsLab's
// C++ port (cpp/platform), with input from another program or from memory
// added for the grain analyzer.
//
// Every program is started without a console window. terminate() ends it
// and every program it started. They also end with the app, however the app
// ends: each program is in a job object that closes with the app. Windows
// only.
#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace fgw::platform {

// A program could not be started.
class ProcessError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

// How much of a program's standard error is kept: its end. ffmpeg can write
// a warning for every frame of a long video, and what is used of it is its
// last lines, so keeping it all would cost memory for nothing.
inline constexpr std::size_t error_tail_bytes = 64 * 1024;

// Adds what a program wrote to standard error to `kept`, which holds its
// end: once `kept` reaches twice error_tail_bytes it is cut to the last
// ones, so the cutting is rare.
void keep_error_tail(std::string& kept, std::string_view written);

// The end of `kept`: at most its last error_tail_bytes, from the start of a
// line (cut mid-line only when one line is longer than that).
std::string error_tail(const std::string& kept);

// `text` with every line ending made "\n" ("\r\n", and a lone "\r"):
// Windows programs end their lines with "\r\n", which would otherwise stay
// inside the app's messages.
std::string universal_newlines(std::string_view text);

class ChildProcess;

struct ProcessOptions {
    // The folder the program runs in; empty is ours.
    std::filesystem::path working_directory;
    // How much the pipe of the program's standard output holds before the
    // program waits for this process to read it; 0 is the system's size.
    std::size_t output_pipe_bytes = 0;
    // Called with each line of the program's standard error as it comes,
    // without its line ending, on the thread that reads it. Its end is kept
    // for standard_error() all the same.
    std::function<void(std::string_view)> on_error_line = nullptr;
    // The program reads what this other program writes to its standard
    // output: that pipe is handed over, so the data never passes through
    // this process, and the other program's read_line() and read() then
    // find nothing.
    ChildProcess* input_from = nullptr;
    // Else, if set, written to the program's standard input on a thread of
    // its own, which then closes it. Else the program reads nothing (NUL).
    std::shared_ptr<const std::string> input_data;
    // Runs at below-normal priority: long work on every core (an encoder)
    // that must not keep the window's thread waiting for a turn. It still
    // gets all the CPU nothing else wants.
    bool background = false;
};

// A program run to its end: its exit code, everything it wrote to standard
// output and the end of its standard error (its lines ended with "\n"), as
// the bytes it wrote (UTF-8 for ffmpeg's tools).
struct ProcessOutput {
    int exit_code = -1;
    std::string standard_output;
    std::string standard_error;
    bool timed_out = false;  // it was ended for taking too long
    bool stopped = false;    // it was ended because the caller's `stop` said so
};

// An exit code as Windows reports it: unsigned (3221225477 for an access
// violation, the number people search for, not -1073741819).
std::string exit_code_text(int exit_code);

// A running program, its standard output read here and its standard error
// collected in the background. Ended (with everything it started) when this
// is destroyed.
class ChildProcess {
public:
    // Starts `arguments[0]` (a path, or a name found on the PATH) with the
    // rest as its arguments. Throws ProcessError if it cannot be started.
    static ChildProcess start(const std::vector<std::string>& arguments, const ProcessOptions& options = {});

    ChildProcess(ChildProcess&&) noexcept;
    ChildProcess& operator=(ChildProcess&&) noexcept;
    ~ChildProcess();

    // The next line of standard output without its line ending, or nothing
    // once the program has closed it.
    std::optional<std::string> read_line();

    // Reads up to `bytes` of standard output into `into`, for a program
    // that writes data rather than lines: how many were read, 0 once the
    // program has closed it.
    std::size_t read(void* into, std::size_t bytes);

    // Waits until `bytes` of standard output are waiting to be read, the
    // program has ended, or `stop` is set, so that one read takes a whole
    // frame: read as it comes, a 4K frame arrives in some 300 pieces, each
    // waking the reading thread. It never waits for more than half of what
    // the pipe holds, or a program waiting for room in a full pipe would be
    // waited for in turn.
    void gather(std::size_t bytes, const std::atomic<bool>& stop);

    // Waits for the program to end, and returns its exit code.
    int wait();

    // Waits up to `timeout`; true if the program has ended.
    bool wait_for(std::chrono::milliseconds timeout);

    // Ends the program, and every program it started.
    void terminate();

    // The end of what the program wrote to standard error (error_tail), its
    // lines ended with "\n" (universal_newlines), complete once it has ended
    // (after wait()).
    std::string standard_error();

    int process_id() const;

private:
    struct State;
    explicit ChildProcess(std::unique_ptr<State> state);
    std::unique_ptr<State> state_;
};

// Runs a program to its end and returns what it wrote. A program still
// running after `timeout`, or once `stop` returns true (asked every 100 ms,
// before the time is checked), is ended, and the result says which. Throws
// ProcessError if it cannot be started.
ProcessOutput run_program(const std::vector<std::string>& arguments, std::chrono::milliseconds timeout,
                          const ProcessOptions& options = {}, const std::function<bool()>& stop = nullptr);

}  // namespace fgw::platform
