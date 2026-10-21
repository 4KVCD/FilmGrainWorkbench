// Film Grain Workbench: the entry point.
//
//   "Film Grain Workbench.exe" [--vvc | --av1]          opens on that tab, else the one used last
//   "Film Grain Workbench.exe" --check-installation FILE writes which programs were found to FILE;
//                                                        exit code 0 when all answer, 1 otherwise
#include <algorithm>
#include <fstream>
#include <memory>

#include <QIcon>

#include <windows.h>
#include <shobjidl.h>

#include "application.h"
#include "fgw/installation.h"
#include "fgw/text.h"
#include "fgw/vvc_grain.h"
#include "settings.h"
#include "theme.h"
#include "workbench.h"

namespace fgw::app {
namespace {

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
