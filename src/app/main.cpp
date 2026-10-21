// Film Grain Workbench: the entry point.
//
//   FilmGrainWorkbench.exe [--vvc | --av1]          opens on that tab, else the one used last
//   FilmGrainWorkbench.exe --check-installation FILE writes which programs were found to FILE;
//                                                    exit code 0 when all answer, 1 otherwise
#include <algorithm>
#include <exception>
#include <fstream>
#include <memory>

#include <QApplication>
#include <QDateTime>
#include <QDir>
#include <QIcon>
#include <QMessageBox>
#include <QPointer>

#include <windows.h>
#include <shobjidl.h>

#include "fgw/installation.h"
#include "fgw/text.h"
#include "fgw/vvc_grain.h"
#include "settings.h"
#include "theme.h"
#include "workbench.h"

namespace fgw::app {
namespace {

// Errors that escape a slot are reported instead of ending the app: written
// to errors.log in the data folder, shown in the tab in view, and in a
// message box. (Work on job threads reports its own errors; this is for the
// window's thread.)
class Application : public QApplication {
public:
    using QApplication::QApplication;

    void set_window(Workbench* window) { window_ = window; }

    bool notify(QObject* receiver, QEvent* event) override {
        try {
            return QApplication::notify(receiver, event);
        } catch (const std::exception& error) {
            report(QString::fromUtf8(error.what()));
        } catch (...) {
            report("an unknown error");
        }
        return false;
    }

private:
    void report(const QString& message) {
        const QString log = QDir(data_folder()).filePath("errors.log");
        QDir().mkpath(data_folder());
        std::ofstream(utf8_path(log.toStdString()), std::ios::app)
            << QDateTime::currentDateTime().toString("yyyy-MM-dd HH:mm:ss").toStdString() << "\n"
            << message.toStdString() << "\n\n";
        if (window_) {
            window_->log("Unexpected error: " + message);
        }
        if (showing_) {  // an error while the box is open: log it only
            return;
        }
        showing_ = true;
        QMessageBox::critical(window_, "Unexpected error",
                              "Something went wrong; the details are in " + QDir::toNativeSeparators(log) +
                                  ".\n\n" + message);
        showing_ = false;
    }

    QPointer<Workbench> window_;
    bool showing_ = false;
};

int check_installation(const QString& report) {
    const std::unique_ptr<QSettings> settings(make_settings(nullptr));
    const std::vector<ProgramCheck> checks = fgw::check_installation(load_tools(*settings), find_vvc_tools());
    const std::string title = (app_title + " " + QApplication::applicationVersion()).toStdString();
    std::ofstream(utf8_path(report.toStdString()), std::ios::binary) << format_installation_report(checks, title) << "\n";
    return std::ranges::all_of(checks, &ProgramCheck::ok) ? 0 : 1;
}

int run(int argc, char* argv[]) {
    Application app(argc, argv);
    // The application and organisation names place the data folder (saved
    // analyses, errors.log): they stay the Python app's. What the user sees is
    // the display name.
    QApplication::setApplicationName(application_name);
    QApplication::setOrganizationName(organisation_name);
    QApplication::setApplicationDisplayName(app_title);
    QApplication::setApplicationVersion(FGW_VERSION);
    const QStringList args = QApplication::arguments().mid(1);
    if (const qsizetype at = args.indexOf("--check-installation"); at >= 0) {
        return at + 1 < args.size() ? check_installation(args[at + 1]) : 2;
    }
    // The same ID in every version, so a pinned taskbar icon keeps working after an update.
    SetCurrentProcessExplicitAppUserModelID(L"4KVCD.FilmGrainWorkbench");
    QApplication::setWindowIcon(QIcon(":/icon.png"));
    apply_theme(app);

    QSettings* settings = make_settings(&app);
    Workbench window(settings);
    if (args.contains("--vvc")) {
        window.show_tab(Workbench::vvc_tab);
    } else if (args.contains("--av1")) {
        window.show_tab(Workbench::av1_tab);
    }
    app.set_window(&window);
    window.show();
    return QApplication::exec();
}

}  // namespace
}  // namespace fgw::app

int main(int argc, char* argv[]) { return fgw::app::run(argc, argv); }
