// Drives the real window like a user, on real files, under a temporary
// profile, and reports what it saw, how long each step took and the longest
// time the window's thread did not answer. A developer tool, not a test.
//
//   fgw_walkthrough av1 SOURCE ENCODE [OUTPUT.mkv]   browse, retune; with OUTPUT also Write and Verify
//   fgw_walkthrough vvc SOURCE [FRAME]               browse, encode a scene, show every view
//   fgw_walkthrough error                            an error in a slot is reported, not fatal
//   fgw_walkthrough keys SOURCE ENCODE               the frame keys against sliders, spin boxes, viewer
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
#include <QDir>
#include <QEventLoop>
#include <QFile>
#include <QFileInfo>
#include <QMessageBox>
#include <QTest>
#include <QTemporaryDir>
#include <QTimer>

#include <windows.h>
#include <psapi.h>

#include "application.h"
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

// The app's own Application (it reports errors), which also times every
// event the window's thread handles, in the CPU time it took that thread,
// and keeps the slowest one: what a stall was spent on.
class TimedApplication : public Application {
public:
    using Application::Application;

    bool notify(QObject* receiver, QEvent* event) override {
        if (depth_++ > 0) {  // an event inside an event: its time counts for the outer one
            const bool handled = Application::notify(receiver, event);
            --depth_;
            return handled;
        }
        const QEvent::Type type = event->type();  // the event may be gone afterwards
        const QString name = QString(receiver->metaObject()->className()) +
                             (receiver->objectName().isEmpty() ? QString() : "/" + receiver->objectName());
        const std::uint64_t start = thread_cycles();
        const bool handled = Application::notify(receiver, event);
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
    QTimer box_closer{nullptr};  // a message box (a failed job's) runs its own loop: closed from there
    QString last_box;   // and its text kept for the report

    // Closes message boxes as a user would, keeping the last one's text.
    void close_message_boxes() {
        QObject::connect(&box_closer, &QTimer::timeout, [this] {
            if (auto* box = qobject_cast<QMessageBox*>(QApplication::activeModalWidget())) {
                last_box = box->text();
                box->close();
            }
        });
        box_closer.start(100);
    }

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
        close_message_boxes();
        const auto idle = [&tab] { return !tab.loading() && !tab.frame_timer_.isActive(); };
        const auto estimated = [&] { return idle() && tab.estimates_.contains(tab.current_index_); };

        begin();
        tab.source_edit_->setText(source);
        tab.load_video(Av1Tab::Kind::source);
        tab.encoded_edit_->setText(encoded);
        tab.load_video(Av1Tab::Kind::encoded);
        // A video that cannot be read, or a frame that cannot be shown, ends the walk.
        const auto failed = [&tab] {
            return tab.source_info_->styleSheet().contains("#ff7b72") || tab.encoded_info_->styleSheet().contains("#ff7b72") ||
                   (!tab.loading() && tab.state_->images().empty() && !tab.state_->placeholder().startsWith("Loading"));
        };
        bool ok = wait([&] { return (estimated() && tab.state_->has(source_view) && tab.state_->has(grain_view)) ||
                                    (idle() && failed()); }, 300) &&
                  tab.state_->has(grain_view);
        report("open both videos, first frame with grain", ok,
               ok ? tab.offset_note_->text()
                  : tab.encoded_info_->text() + " | " + tab.state_->placeholder().section('\n', 0, 2));
        if (!ok) {
            return;
        }

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
            leave_loading([&] { tab.go_to(tab.current_index_ / 2); tab.step(1); });
            return;
        }
        begin();
        tab.confirm_long_runs = false;
        tab.output_edit_->setText(output);
        tab.write();
        ok = wait([&] { return !tab.busy(); }, 3600) && tab.written_.has_value();
        report("Write (every frame)", ok,
               (ok ? tab.write_label_->text() : tab.status_->label()->text() + " " + last_box).left(200));
        if (!ok) {
            return;
        }

        begin();
        ok = wait(idle) && (tab.verify(), wait([&] { return !tab.busy(); }, 300));
        ok = ok && tab.write_label_->text().contains("matches the preview exactly");
        report("Verify", ok, tab.write_label_->text().left(140));
        leave_loading([&] { tab.go_to(tab.current_index_ / 2); tab.step(1); });
    }

    // Asks for frames, a second one queued behind the first, and returns
    // without waiting: the window then closes while a frame loads (closing
    // used to start the queued frame's job, then destroy it running).
    void leave_loading(const std::function<void()>& ask) {
        begin();
        ask();
        wait([] { return false; }, 0.05);  // the frame timer starts the first
        ask();
        report("asked for frames, closing while they load", true);
    }

    void vvc(const QString& source, std::int64_t frame) {
        VvcTab& tab = *window.vvc();
        window.show_tab(Workbench::vvc_tab);
        close_message_boxes();
        const auto idle = [&tab] { return !tab.busy() && !tab.frame_timer_.isActive(); };

        begin();
        tab.source_edit_->setText(source);
        tab.load_source();
        const auto shows_error = [&tab] { return tab.source_info_->styleSheet().contains("#ff7b72"); };
        bool ok = wait([&] { return idle() && (tab.state_->has(source_view) || shows_error()); }, 300) &&
                  tab.state_->has(source_view);
        report("open the source", ok, tab.source_info_->text().left(100));
        if (!ok) {
            return;
        }

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
        leave_loading([&] { tab.go_to(tab.current_index_ / 2); tab.step(1); });
    }

    // An exception from a slot goes to errors.log, the activity log and a
    // message box (closed here as a user would), and the app carries on.
    void error() {
        window.show_tab(Workbench::av1_tab);
        begin();
        QTimer closer;  // the message box runs its own event loop: close it from there
        bool box_shown = false;
        QObject::connect(&closer, &QTimer::timeout, [&box_shown] {
            if (QWidget* box = QApplication::activeModalWidget()) {
                box_shown = true;
                box->close();
            }
        });
        closer.start(100);
        QTimer::singleShot(0, [] { throw std::runtime_error("a bug in a slot"); });
        const QString log_file = QDir(data_folder()).filePath("errors.log");
        const auto logged = [&log_file] {
            QFile file(log_file);
            return file.open(QIODevice::ReadOnly) && file.readAll().contains("a bug in a slot");
        };
        bool ok = wait([&] { return box_shown && logged(); }, 10);
        ok = ok && window.av1()->log_view_->toPlainText().contains("Unexpected error: a bug in a slot");
        report("error in a slot: errors.log, log, message box", ok, log_file);
        begin();
        window.show_tab(Workbench::vvc_tab);
        wait([] { return false; }, 0.3);
        report("the app carries on", window.isVisible() && window.vvc()->isVisible());
    }

    // Real key presses (Qt Test goes through the shortcut map as a keyboard
    // does): the frame keys leave a focused slider or spin box its own keys,
    // and a pop-out viewer keeps its keys when the other tab is shown.
    void keys(const QString& source, const QString& encoded) {
        Av1Tab& tab = *window.av1();
        begin();
        window.show_tab(Workbench::av1_tab);
        window.activateWindow();
        static_cast<void>(QTest::qWaitForWindowActive(&window));
        tab.source_edit_->setText(source);
        tab.load_video(Av1Tab::Kind::source);
        tab.encoded_edit_->setText(encoded);
        tab.load_video(Av1Tab::Kind::encoded);
        const bool loaded = wait([&] { return !tab.loading() && tab.state_->has(grain_view); }, 120);
        report("open both videos", loaded);
        if (!loaded) {
            return;
        }

        begin();
        tab.set_mode(grain_view);
        tab.luma_slider_->setFocus();
        const int before = tab.luma_slider_->value();
        QTest::keyClick(tab.luma_slider_, Qt::Key_Left);
        bool ok = tab.luma_slider_->value() == before - 1 && tab.state_->mode() == grain_view;
        report("← on the focused strength slider moves the slider", ok,
               QString("slider %1 → %2, view %3").arg(before).arg(tab.luma_slider_->value()).arg(tab.state_->mode()));

        begin();
        tab.seed_spin_->setFocus();
        tab.seed_spin_->selectAll();
        QTest::keyClicks(tab.seed_spin_, "1234");
        ok = tab.seed_spin_->text() == "1234" && tab.state_->mode() == grain_view;
        report("digits typed into the seed box go into it", ok,
               QString("seed '%1', view %2").arg(tab.seed_spin_->text()).arg(tab.state_->mode()));

        begin();
        tab.pane_->view()->setFocus();
        QTest::keyClick(tab.pane_->view(), Qt::Key_Left);
        ok = tab.state_->mode() == encoded_view;
        report("← on the picture shows the encode", ok, QString("view %1").arg(tab.state_->mode()));

        begin();
        tab.open_viewer_window();
        window.show_tab(Workbench::vvc_tab);
        // The offscreen platform makes no second window active, so a key
        // cannot reach the viewer here: its keys must be on, the hidden
        // page's keys in the main window off.
        const auto enabled = [](const QObject* owner, const QWidget* in_window) {
            int on = 0;
            int all = 0;
            for (const QShortcut* shortcut : owner->findChildren<QShortcut*>()) {
                if (qobject_cast<QWidget*>(shortcut->parent())->window() == in_window) {
                    ++all;
                    on += shortcut->isEnabled() ? 1 : 0;
                }
            }
            return std::pair{on, all};
        };
        const auto [viewer_on, viewer_all] = enabled(tab.viewer_window_, tab.viewer_window_);
        const auto [page_on, page_all] = enabled(&tab, &window);
        ok = viewer_all > 0 && viewer_on == viewer_all && page_all > 0 && page_on == 0;
        report("with the VVC tab shown: AV1 viewer keys on, AV1 page keys off", ok,
               QString("viewer %1 of %2 on, page %3 of %4 on").arg(viewer_on).arg(viewer_all).arg(page_on).arg(page_all));
        tab.viewer_window_->close();
    }
};

}  // namespace fgw::app

namespace {

const std::wstring tests_key = L"Software\\FilmGrainWorkbench-tests";

// Deletes a profile's registry key, and the tests' key once no profile is left in it.
void delete_settings_key(const QString& profile_name) {
    RegDeleteTreeW(HKEY_CURRENT_USER, (tests_key + L"\\" + profile_name.toStdWString()).c_str());
    RegDeleteKeyW(HKEY_CURRENT_USER, tests_key.c_str());  // fails, as it should, while it has subkeys
}

// The profiles of walkthroughs that were stopped before their end.
void delete_old_profiles() {
    for (const QFileInfo& folder : QDir(QDir::tempPath()).entryInfoList({"fgw-walkthrough-*"}, QDir::Dirs)) {
        QDir(folder.filePath()).removeRecursively();
        delete_settings_key(folder.fileName());
    }
}

}  // namespace

int main(int argc, char* argv[]) {
    using namespace fgw::app;
    delete_old_profiles();
    QTemporaryDir profile(QDir::tempPath() + "/fgw-walkthrough-XXXXXX");
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
    if (args.isEmpty() || ((args[0] == "av1" || args[0] == "keys") && args.size() < 3) ||
        (args[0] == "vvc" && args.size() < 2) || !QStringList{"av1", "vvc", "error", "keys"}.contains(args[0])) {
        std::fprintf(stderr, "usage: fgw_walkthrough av1 SOURCE ENCODE [OUTPUT.mkv] | vvc SOURCE [FRAME] | error | "
                             "keys SOURCE ENCODE\n");
        return 2;
    }
    QSettings* settings = make_settings(&app);
    auto* window = new Workbench(settings);
    window->resize(1600, 1000);
    window->show();
    app.set_window(window);
    Walkthrough walk{*window, app, {}, {}, 0};
    walk.wait([] { return false; }, 0.5);  // let the tabs start up
    if (args[0] == "av1") {
        walk.av1(args[1], args[2], args.value(3));
    } else if (args[0] == "vvc") {
        walk.vvc(args[1], args.size() > 2 ? args[2].toLongLong() : -1);
    } else if (args[0] == "keys") {
        walk.keys(args[1], args[2]);
    } else {
        walk.error();
    }
    walk.begin();
    window->close();
    delete window;
    walk.report("close", true);

    delete settings;
    delete_settings_key(QFileInfo(profile.path()).fileName());
    return walk.failures ? 1 : 0;
}
