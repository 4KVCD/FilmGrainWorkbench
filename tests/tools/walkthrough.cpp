// Drives the real window like a user, on real files, under a temporary
// profile, and reports what it saw, how long each step took and the longest
// time the window's thread did not answer. A developer tool, not a test.
//
//   fgw_walkthrough av1 SOURCE ENCODE [OUTPUT.mkv]   browse, retune; with OUTPUT also Write and Verify
//   fgw_walkthrough vvc SOURCE [FRAME]               browse, encode a scene, show every view
//
// A small pair that writes in seconds (49 frames of noisy test pattern, the
// encode with audio and subtitles), into tests\.cache:
//   ffmpeg -f lavfi -i testsrc2=size=320x184:rate=24:duration=2 -vf noise=c0s=12:c0f=t+u,format=yuv420p
//          -c:v ffv1 grainy_source.mkv
//   ffmpeg -f lavfi -i testsrc2=size=320x184:rate=24:duration=2 -f lavfi -i sine=frequency=440:duration=2
//          -i sub.srt -vf noise=c0s=12:c0f=t+u,format=yuv420p -map 0 -map 1 -map 2
//          -c:v libvvenc -preset faster -qp 37 -c:a aac -c:s srt grainy_vvc.mkv
// (sub.srt: "1", "00:00:00,500 --> 00:00:01,500", "hello", one per line.)
#include <chrono>
#include <cstdio>
#include <functional>

#include <QApplication>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFileInfo>
#include <QTemporaryDir>
#include <QTimer>

#include <windows.h>
#include <psapi.h>

#include "av1_tab.h"
#include "settings.h"
#include "theme.h"
#include "vvc_tab.h"
#include "workbench.h"

namespace fgw::app {

// Ticks every 5 ms on the window's thread; a late tick is time the window
// could not answer.
class StallMeter {
public:
    StallMeter() {
        timer_.setInterval(5);
        QObject::connect(&timer_, &QTimer::timeout, [this] {
            const qint64 now = clock_.nsecsElapsed();
            worst_ = std::max(worst_, now - last_);
            last_ = now;
        });
        clock_.start();
        timer_.start();
    }
    // The longest gap between ticks since the last call, in ms.
    double take_worst_ms() {
        const double worst = static_cast<double>(worst_) / 1e6;
        worst_ = 0;
        return worst;
    }

private:
    QTimer timer_;
    QElapsedTimer clock_;
    qint64 last_ = 0;
    qint64 worst_ = 0;
};

// Times every event the window's thread handles, in the CPU time it took
// that thread, and keeps the slowest one: what a stall was spent on.
class TimedApplication : public QApplication {
public:
    using QApplication::QApplication;

    bool notify(QObject* receiver, QEvent* event) override {
        if (depth_++ > 0) {  // an event inside an event: its time counts for the outer one
            const bool handled = QApplication::notify(receiver, event);
            --depth_;
            return handled;
        }
        const QEvent::Type type = event->type();  // the event may be gone afterwards
        const QString name = QString(receiver->metaObject()->className()) +
                             (receiver->objectName().isEmpty() ? QString() : "/" + receiver->objectName());
        const std::uint64_t start = thread_cycles();
        const bool handled = QApplication::notify(receiver, event);
        --depth_;
        if (const double ms = static_cast<double>(thread_cycles() - start) / cycles_per_ms_; ms > slowest_ms_) {
            slowest_ms_ = ms;
            slowest_ = QString("%1 event %2").arg(name).arg(static_cast<int>(type));
        }
        return handled;
    }

    // The slowest event since the last call: "12.3 ms Receiver event 12".
    QString take_slowest() {
        const QString text = slowest_ms_ > 0 ? QString("%1 ms %2").arg(slowest_ms_, 0, 'f', 1).arg(slowest_) : QString();
        slowest_ms_ = 0;
        return text;
    }

private:
    // The cycles the window's thread ran: unlike the clock, they do not count
    // the time other programs had the CPU.
    static std::uint64_t thread_cycles() {
        ULONG64 cycles = 0;
        QueryThreadCycleTime(GetCurrentThread(), &cycles);
        return cycles;
    }

    // Cycles per ms, from a 30 ms busy loop.
    static double measure_cycles_per_ms() {
        QElapsedTimer clock;
        clock.start();
        const std::uint64_t start = thread_cycles();
        while (clock.nsecsElapsed() < 30'000'000) {
        }
        return static_cast<double>(thread_cycles() - start) / (static_cast<double>(clock.nsecsElapsed()) / 1e6);
    }

    double cycles_per_ms_ = measure_cycles_per_ms();
    int depth_ = 0;
    double slowest_ms_ = 0;
    QString slowest_;
};

// The process's working set now and at most, in MB.
QString memory_text() {
    PROCESS_MEMORY_COUNTERS_EX counters{};
    counters.cb = sizeof counters;
    GetProcessMemoryInfo(GetCurrentProcess(), reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&counters), sizeof counters);
    return QString("RAM %1 MB (peak %2)")
        .arg(counters.WorkingSetSize >> 20)
        .arg(counters.PeakWorkingSetSize >> 20);
}

struct Walkthrough {
    Workbench& window;
    TimedApplication& app;
    StallMeter stalls;
    QElapsedTimer step_clock;
    int failures = 0;

    // Runs the event loop until `done` holds, up to `seconds`.
    bool wait(const std::function<bool()>& done, double seconds = 120) {
        QElapsedTimer clock;
        clock.start();
        while (!done()) {
            if (clock.elapsed() > seconds * 1000) {
                return false;
            }
            QEventLoop loop;
            QTimer::singleShot(10, &loop, &QEventLoop::quit);
            loop.exec();
        }
        return true;
    }

    void begin() {
        step_clock.start();
        stalls.take_worst_ms();
        app.take_slowest();
    }

    // Prints one step's result: "ok" or "FAILED", its time, the longest
    // stall and the slowest event, the memory, and what the window showed.
    void report(const char* step, bool ok, const QString& detail = {}) {
        failures += ok ? 0 : 1;
        std::printf("%-6s %-40s %7.2f s  stall %5.1f ms  %s\n       slowest: %s  %s\n", ok ? "ok" : "FAILED", step,
                    static_cast<double>(step_clock.elapsed()) / 1000, stalls.take_worst_ms(),
                    detail.toUtf8().constData(), app.take_slowest().toUtf8().constData(),
                    memory_text().toUtf8().constData());
        std::fflush(stdout);
    }

    void av1(const QString& source, const QString& encoded, const QString& output) {
        Av1Tab& tab = *window.av1();
        window.show_tab(Workbench::av1_tab);
        const auto idle = [&tab] { return !tab.loading() && !tab.frame_timer_.isActive(); };
        const auto estimated = [&] { return idle() && tab.estimates_.contains(tab.current_index_); };

        begin();
        tab.source_edit_->setText(source);
        tab.load_video(Av1Tab::Kind::source);
        tab.encoded_edit_->setText(encoded);
        tab.load_video(Av1Tab::Kind::encoded);
        bool ok = wait([&] { return estimated() && tab.state_->has(source_view) && tab.state_->has(grain_view); }, 300);
        report("open both videos, first frame with grain", ok, tab.offset_note_->text());

        begin();
        const std::int64_t start = tab.current_index_;
        for (int i = 0; i < 5; ++i) {
            tab.step(1);
        }
        ok = wait([&] { return estimated() && tab.current_index_ == start + 5; });
        report("step 5 frames quickly", ok, tab.caption(tab.current_index_));

        begin();
        tab.go_to(tab.encoded_->frame_count() / 2);
        ok = wait(estimated, 300);
        report("seek to the middle", ok, tab.caption(tab.current_index_));

        begin();
        bool modes = true;
        for (const View view : all_views) {
            tab.set_mode(view);
            modes = modes && tab.state_->mode() == view;
            wait([] { return false; }, 0.1);  // painted, as a user would see it
        }
        report("every view", modes);

        begin();
        tab.offset_spin_->setValue(tab.offset_spin_->value() + 1);
        ok = wait([&] { return !tab.offset_timer_.isActive() && estimated(); });
        const QString off = tab.offset_note_->text();
        tab.offset_spin_->setValue(tab.offset_spin_->value() - 1);
        ok = ok && wait([&] { return !tab.offset_timer_.isActive() && estimated(); });
        report("offset +1 and back", ok, off.left(60) + " / " + tab.offset_note_->text().left(60));

        begin();
        tab.luma_spin_->setValue(250);
        ok = wait([&] { return idle() && !tab.tune_timer_.isActive(); });
        report("luma strength 250 %", ok, tab.tune_label_->text().left(100));

        if (output.isEmpty()) {
            return;
        }
        begin();
        tab.confirm_long_runs = false;
        tab.output_edit_->setText(output);
        tab.write();
        ok = wait([&] { return !tab.busy() && tab.written_.has_value(); }, 3600);
        report("Write (every frame)", ok, tab.write_label_->text().left(140));

        begin();
        ok = wait(idle) && (tab.verify(), wait([&] { return !tab.busy(); }, 300));
        ok = ok && tab.write_label_->text().contains("matches the preview exactly");
        report("Verify", ok, tab.write_label_->text().left(140));
    }

    void vvc(const QString& source, std::int64_t frame) {
        VvcTab& tab = *window.vvc();
        window.show_tab(Workbench::vvc_tab);
        const auto idle = [&tab] { return !tab.busy() && !tab.frame_timer_.isActive(); };

        begin();
        tab.source_edit_->setText(source);
        tab.load_source();
        bool ok = wait([&] { return idle() && tab.state_->has(source_view); }, 300);
        report("open the source", ok, tab.source_info_->text().left(100));

        begin();
        tab.go_to(frame >= 0 ? frame : tab.source_->frame_count() / 3);
        ok = wait([&] { return idle() && tab.state_->has(source_view); });
        report("seek", ok, tab.caption(tab.current_index_));

        begin();
        tab.encode_scene();
        ok = wait([&] { return tab.state_->has(grain_view); }, 1800);
        report("encode this scene: first mode shown", ok, tab.status_->label()->text().left(100));

        begin();
        ok = wait([&] { return idle() && tab.scene_ && tab.scene_->clips.size() == 3; }, 3600);
        report("all three modes encoded", ok, tab.scene_label_->text().left(100));

        begin();
        bool modes = true;
        for (const View view : all_views) {
            tab.set_mode(view);
            modes = modes && tab.state_->mode() == view;
            wait([] { return false; }, 0.1);  // painted, as a user would see it
        }
        report("every view", modes);

        begin();
        for (int i = 0; i < 5; ++i) {
            tab.step(1);
        }
        ok = wait([&] { return idle() && tab.state_->has(grain_view); });
        report("step 5 frames in the scene", ok, tab.caption(tab.current_index_));
        // The side panel's numbers: to compare runs before and after a change.
        std::printf("panel:\n%s\n", tab.info_label_->text().toUtf8().constData());
    }
};

}  // namespace fgw::app

int main(int argc, char* argv[]) {
    using namespace fgw::app;
    QTemporaryDir profile;
    qputenv("FGW_PROFILE", profile.path().toLocal8Bit());
    if (qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM")) {
        qputenv("QT_QPA_PLATFORM", "offscreen");  // nothing appears on the screen
    }
    TimedApplication app(argc, argv);
    QApplication::setApplicationName(application_name);
    QApplication::setOrganizationName(organisation_name);
    QApplication::setApplicationVersion(FGW_VERSION);
    apply_theme(app);
    const QStringList args = QApplication::arguments().mid(1);
    if (args.size() < 2 || (args[0] == "av1" && args.size() < 3)) {
        std::fprintf(stderr, "usage: fgw_walkthrough av1 SOURCE ENCODE [OUTPUT.mkv] | vvc SOURCE [FRAME]\n");
        return 2;
    }
    QSettings* settings = make_settings(&app);
    auto* window = new Workbench(settings);
    window->resize(1600, 1000);
    window->show();
    Walkthrough walk{*window, app, {}, {}, 0};
    walk.wait([] { return false; }, 0.5);  // let the tabs start up
    if (args[0] == "av1") {
        walk.av1(args[1], args[2], args.value(3));
    } else {
        walk.vvc(args[1], args.size() > 2 ? args[2].toLongLong() : -1);
    }
    walk.begin();
    window->close();
    delete window;
    walk.report("close", true);

    // The profile's registry key, and the tests' key once no profile is left in it.
    delete settings;
    const std::wstring tests = L"Software\\FilmGrainWorkbench-tests";
    RegDeleteTreeW(HKEY_CURRENT_USER, (tests + L"\\" + QFileInfo(profile.path()).fileName().toStdWString()).c_str());
    RegDeleteKeyW(HKEY_CURRENT_USER, tests.c_str());  // fails, as it should, while it has subkeys
    return walk.failures ? 1 : 0;
}
