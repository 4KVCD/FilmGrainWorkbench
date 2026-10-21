// ChildProcess on Windows: CreateProcessW with pipes, in a job object of its
// own.
//
// Each program is put in a job object that ends every process in it when
// the job's last handle closes (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE). The
// handle is the app's alone, so the program and whatever it starts end with
// the app, however the app ends, and terminate() ends them together.
//
// Only the program's own pipe ends are handed down to it
// (PROC_THREAD_ATTRIBUTE_HANDLE_LIST). Handed
// down by plain inheritance, two programs started at the same moment could
// each take a copy of the other's pipe, and a reader would then wait for an
// end of output that never comes.
#include "fgw/platform/process.h"

#include <algorithm>
#include <cctype>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <string_view>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

#include "error_lines.h"
#include "text_windows.h"

namespace fgw::platform {

namespace {

// A Windows handle, closed when it goes.
class Handle {
public:
    Handle() = default;
    explicit Handle(HANDLE handle) : handle_(handle == INVALID_HANDLE_VALUE ? nullptr : handle) {}
    Handle(Handle&& other) noexcept : handle_(other.release()) {}
    Handle& operator=(Handle&& other) noexcept {
        if (this != &other) {
            reset(other.release());
        }
        return *this;
    }
    ~Handle() { reset(); }

    HANDLE get() const { return handle_; }
    explicit operator bool() const { return handle_ != nullptr; }
    HANDLE release() { return std::exchange(handle_, nullptr); }
    void reset(HANDLE handle = nullptr) {
        if (handle_ != nullptr) {
            CloseHandle(handle_);
        }
        handle_ = handle;
    }

private:
    HANDLE handle_ = nullptr;
};

[[noreturn]] void fail(const std::string& what) {
    throw ProcessError(what + " (Windows error " + std::to_string(GetLastError()) + ")");
}

// One argument quoted as the Microsoft C run-time reads a command line back
// into arguments (CommandLineToArgvW): in double quotes when it has a space,
// a tab or a quote or is empty, with its quotes and the backslashes before
// them escaped.
std::wstring quote_argument(const std::wstring& argument) {
    if (!argument.empty() && argument.find_first_of(L" \t\n\v\"") == std::wstring::npos) {
        return argument;
    }
    std::wstring quoted = L"\"";
    std::size_t backslashes = 0;
    for (const wchar_t c : argument) {
        if (c == L'\\') {
            ++backslashes;
            continue;
        }
        // Backslashes count double before a quote, which itself is escaped.
        quoted.append(c == L'"' ? backslashes * 2 + 1 : backslashes, L'\\');
        quoted.push_back(c);
        backslashes = 0;
    }
    quoted.append(backslashes * 2, L'\\');  // before the closing quote
    quoted.push_back(L'"');
    return quoted;
}

std::wstring command_line(const std::vector<std::string>& arguments) {
    std::wstring line;
    for (const std::string& argument : arguments) {
        if (!line.empty()) {
            line.push_back(L' ');
        }
        line += quote_argument(widen(argument));
    }
    return line;
}

// A pipe one of whose ends is handed to the program.
struct Pipe {
    Handle read;
    Handle write;
};

enum class ProgramEnd { write, read };

// `bytes` is how much it holds; 0 is the system's size. Only the program's
// end can be inherited.
Pipe make_pipe(ProgramEnd program_end = ProgramEnd::write, std::size_t bytes = 0) {
    SECURITY_ATTRIBUTES inheritable{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    HANDLE read = nullptr;
    HANDLE write = nullptr;
    if (!CreatePipe(&read, &write, &inheritable, static_cast<DWORD>(std::min<std::size_t>(bytes, 1u << 30)))) {
        fail("a pipe could not be made");
    }
    Pipe pipe{Handle(read), Handle(write)};
    const Handle& ours = program_end == ProgramEnd::write ? pipe.read : pipe.write;
    SetHandleInformation(ours.get(), HANDLE_FLAG_INHERIT, 0);
    return pipe;
}

// Writes all of `data` to `pipe`, then closes it. Stops early when the
// program has closed its end (it ended, or read no further).
void write_all(Handle pipe, std::shared_ptr<const std::string> data) {
    std::size_t done = 0;
    while (done < data->size()) {
        DWORD written = 0;
        const DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(data->size() - done, 1u << 24));
        if (!WriteFile(pipe.get(), data->data() + done, chunk, &written, nullptr) || written == 0) {
            return;
        }
        done += written;
    }
}

// A job object that ends its processes when its last handle closes.
Handle make_job() {
    Handle job(CreateJobObjectW(nullptr, nullptr));
    if (!job) {
        fail("a job object could not be made");
    }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job.get(), JobObjectExtendedLimitInformation, &limits, sizeof limits)) {
        fail("a job object could not be set up");
    }
    return job;
}

}  // namespace

struct ChildProcess::State {
    Handle process;
    Handle job;
    Handle output;  // the program's standard output, read by read_line
    DWORD id = 0;
    std::string unread;  // read from the pipe, not yet given as lines
    bool output_ended = false;
    // For gather's waits of a millisecond: a plain Sleep(1) can last a
    // whole 15.6 ms tick.
    Handle timer;
    // Writes ProcessOptions::input_data to the program, if it was given.
    std::thread input_writer;

    // Standard error, read on a thread of its own so the program never
    // waits on a full pipe; its end is kept (keep_error_tail).
    std::thread error_reader;
    std::mutex error_mutex;
    std::condition_variable error_ended_signal;
    std::string error_text;
    bool error_ended = false;

    ~State() {
        if (process) {
            TerminateJobObject(job.get(), 1);
        }
        if (error_reader.joinable()) {
            error_reader.join();
        }
        if (input_writer.joinable()) {  // its writes fail once the program has ended
            input_writer.join();
        }
    }
};

ChildProcess ChildProcess::start(const std::vector<std::string>& arguments, const ProcessOptions& options) {
    if (arguments.empty()) {
        throw ProcessError("no program to start");
    }
    // The job first: made after the program, a job that could not be made
    // left the program started suspended, for good.
    Handle job = make_job();
    Pipe output = make_pipe(ProgramEnd::write, options.output_pipe_bytes);
    Pipe error = make_pipe();
    Handle input;
    Handle input_writer;  // our end of the pipe input_data goes through
    if (options.input_from != nullptr) {
        State& source = *options.input_from->state_;
        input = std::move(source.output);
        source.output_ended = true;
        SetHandleInformation(input.get(), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    } else if (options.input_data) {
        Pipe pipe = make_pipe(ProgramEnd::read);
        input = std::move(pipe.read);
        input_writer = std::move(pipe.write);
    } else {
        SECURITY_ATTRIBUTES inheritable{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
        input = Handle(CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &inheritable,
                                   OPEN_EXISTING, 0, nullptr));
    }
    if (!input) {
        fail("the program's standard input could not be opened");
    }

    // The three handles the program is given, and no others.
    HANDLE handed_down[3] = {input.get(), output.write.get(), error.write.get()};
    SIZE_T list_size = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &list_size);
    std::vector<char> list_memory(list_size);
    auto* attributes = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(list_memory.data());
    if (!InitializeProcThreadAttributeList(attributes, 1, 0, &list_size) ||
        !UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handed_down,
                                   sizeof handed_down, nullptr, nullptr)) {
        fail("the program's handles could not be listed");
    }

    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof startup;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = input.get();
    startup.StartupInfo.hStdOutput = output.write.get();
    startup.StartupInfo.hStdError = error.write.get();
    startup.lpAttributeList = attributes;

    std::wstring line = command_line(arguments);
    const std::wstring folder = options.working_directory.empty() ? std::wstring() : options.working_directory.wstring();
    PROCESS_INFORMATION started{};
    // Started suspended, so that it is in its job before it can start
    // anything of its own.
    const BOOL made = CreateProcessW(nullptr, line.data(), nullptr, nullptr, TRUE,
                                     CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT |
                                         CREATE_UNICODE_ENVIRONMENT,
                                     nullptr, folder.empty() ? nullptr : folder.c_str(), &startup.StartupInfo,
                                     &started);
    const DWORD start_error = made ? ERROR_SUCCESS : GetLastError();
    DeleteProcThreadAttributeList(attributes);
    if (!made) {
        // "[WinError 2] The system cannot find the file specified" when
        // ffmpeg is not where it was looked for.
        std::string reason = std::system_category().message(static_cast<int>(start_error));
        while (!reason.empty() && (reason.back() == '.' || std::isspace(static_cast<unsigned char>(reason.back())))) {
            reason.pop_back();
        }
        throw ProcessError("[WinError " + std::to_string(start_error) + "] " + reason);
    }
    auto state = std::make_unique<State>();
    state->process = Handle(started.hProcess);
    Handle thread(started.hThread);
    state->id = started.dwProcessId;
    state->job = std::move(job);
    if (!AssignProcessToJobObject(state->job.get(), state->process.get())) {
        TerminateProcess(state->process.get(), 1);
        fail("could not put " + arguments.front() + " in a job object");
    }
    ResumeThread(thread.get());

    // The program has its copies; ours must close, or the pipes never end.
    input.reset();
    output.write.reset();
    error.write.reset();
    state->output = std::move(output.read);
    if (input_writer) {
        state->input_writer = std::thread(write_all, std::move(input_writer), options.input_data);
    }
    state->error_reader = std::thread([raw = state.get(), pipe = std::move(error.read),
                                       lines = ErrorLines(options.on_error_line)]() mutable {
        char buffer[4096];
        DWORD count = 0;
        while (ReadFile(pipe.get(), buffer, sizeof buffer, &count, nullptr) && count > 0) {
            lines.add(std::string_view(buffer, count));
            const std::lock_guard lock(raw->error_mutex);
            keep_error_tail(raw->error_text, std::string_view(buffer, count));
        }
        lines.finish();
        const std::lock_guard lock(raw->error_mutex);
        raw->error_ended = true;
        raw->error_ended_signal.notify_all();
    });
    return ChildProcess(std::move(state));
}

ChildProcess::ChildProcess(std::unique_ptr<State> state) : state_(std::move(state)) {}
ChildProcess::ChildProcess(ChildProcess&&) noexcept = default;
ChildProcess& ChildProcess::operator=(ChildProcess&&) noexcept = default;
ChildProcess::~ChildProcess() = default;

std::optional<std::string> ChildProcess::read_line() {
    while (true) {
        const std::size_t end = state_->unread.find('\n');
        if (end != std::string::npos) {
            std::string line = state_->unread.substr(0, end);
            state_->unread.erase(0, end + 1);
            if (!line.empty() && line.back() == '\r') {
                line.pop_back();
            }
            return line;
        }
        if (state_->output_ended) {
            if (state_->unread.empty()) {
                return std::nullopt;
            }
            return std::exchange(state_->unread, std::string());
        }
        char buffer[4096];
        DWORD count = 0;
        if (ReadFile(state_->output.get(), buffer, sizeof buffer, &count, nullptr) && count > 0) {
            state_->unread.append(buffer, count);
        } else {
            state_->output_ended = true;
        }
    }
}

std::size_t ChildProcess::read(void* into, std::size_t bytes) {
    if (bytes == 0) {
        return 0;
    }
    if (!state_->unread.empty()) {  // what read_line read ahead
        const std::size_t count = std::min(bytes, state_->unread.size());
        std::memcpy(into, state_->unread.data(), count);
        state_->unread.erase(0, count);
        return count;
    }
    if (state_->output_ended) {
        return 0;
    }
    DWORD count = 0;
    const DWORD wanted = static_cast<DWORD>(std::min<std::size_t>(bytes, 1u << 30));
    if (ReadFile(state_->output.get(), into, wanted, &count, nullptr) && count > 0) {
        return count;
    }
    state_->output_ended = true;
    return 0;
}

void ChildProcess::gather(std::size_t bytes, const std::atomic<bool>& stop) {
    if (!state_->unread.empty() || state_->output_ended) {
        return;
    }
    DWORD held = 0;
    if (!GetNamedPipeInfo(state_->output.get(), nullptr, nullptr, &held, nullptr)) {
        return;
    }
    const std::size_t wanted = std::min<std::size_t>(bytes, held / 2);
    if (!state_->timer) {
        state_->timer.reset(
            CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS));
        if (!state_->timer) {  // before Windows 10 1803
            state_->timer.reset(CreateWaitableTimerExW(nullptr, nullptr, 0, TIMER_ALL_ACCESS));
        }
    }
    while (!stop) {
        DWORD available = 0;
        if (!PeekNamedPipe(state_->output.get(), nullptr, 0, nullptr, &available, nullptr)) {
            return;  // closed and empty: the read says so
        }
        if (available >= wanted || WaitForSingleObject(state_->process.get(), 0) == WAIT_OBJECT_0) {
            return;
        }
        LARGE_INTEGER due;
        due.QuadPart = -10'000;  // a millisecond from now, in 100 ns units
        if (state_->timer && SetWaitableTimer(state_->timer.get(), &due, 0, nullptr, nullptr, FALSE)) {
            WaitForSingleObject(state_->timer.get(), INFINITE);
        } else {
            Sleep(1);
        }
    }
}

std::string exit_code_text(int exit_code) { return std::to_string(static_cast<std::uint32_t>(exit_code)); }

int ChildProcess::wait() {
    WaitForSingleObject(state_->process.get(), INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(state_->process.get(), &code);
    return static_cast<int>(code);
}

bool ChildProcess::wait_for(std::chrono::milliseconds timeout) {
    return WaitForSingleObject(state_->process.get(), static_cast<DWORD>(timeout.count())) == WAIT_OBJECT_0;
}

void ChildProcess::terminate() { TerminateJobObject(state_->job.get(), 1); }

std::string ChildProcess::standard_error() {
    std::unique_lock lock(state_->error_mutex);
    // Once the program has ended its error output ends with it, unless a
    // program it started still holds the pipe: then what came is enough.
    state_->error_ended_signal.wait_for(lock, std::chrono::seconds(5), [this] { return state_->error_ended; });
    return universal_newlines(error_tail(state_->error_text));
}

int ChildProcess::process_id() const { return static_cast<int>(state_->id); }

}  // namespace fgw::platform
