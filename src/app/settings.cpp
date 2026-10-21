#include "settings.h"

#include <map>

#include <QDir>
#include <QFileInfo>
#include <QStandardPaths>

namespace fgw::app {

namespace {

QString test_profile() { return qEnvironmentVariable("FGW_PROFILE"); }

}  // namespace

QSettings* make_settings(QObject* parent) {
    if (const QString profile = test_profile(); !profile.isEmpty()) {
        // In the registry like the real settings, so a test measures what the
        // app does (an INI file is flushed to disk on every save).
        return new QSettings(test_settings_key(profile), QSettings::NativeFormat, parent);
    }
    return new QSettings(organisation_name, settings_application, parent);
}

QString test_settings_key(const QString& profile) {
    return R"(HKEY_CURRENT_USER\Software\FilmGrainWorkbench-tests\)" + QFileInfo(profile).fileName();
}

QString data_folder() {
    const QString profile = test_profile();
    return !profile.isEmpty() ? QDir(profile).filePath("data")
                              : QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
}

Tools load_tools(const QSettings& settings) {
    const Tools found = detect_tools();
    Tools tools;
    for (const std::string& name : Tools::names()) {
        const QString saved = settings.value("tools/" + QString::fromStdString(name)).toString();
        tools.path(name) = !saved.isEmpty() && QFileInfo(saved).isFile() ? saved.toStdString() : found.path(name);
    }
    return tools;
}

void save_tools(QSettings& settings, const Tools& tools) {
    for (const std::string& name : Tools::names()) {
        settings.setValue("tools/" + QString::fromStdString(name), QString::fromStdString(tools.path(name)));
    }
}

void migrate_settings(QSettings& settings) {
    static const std::map<QString, QString> renamed = {{"window/splitter", "av1/splitter"},
                                                       {"files/source", "av1/source"},
                                                       {"files/encoded", "av1/encoded"},
                                                       {"viewer/geometry", "av1/viewer_geometry"},
                                                       {"window/geometry", ""}};  // no longer used
    static const QStringList moved_groups = {"tune", "analysis", "offsets"};
    for (const QString& old_key : settings.allKeys()) {
        QString new_key;
        if (const auto known = renamed.find(old_key); known != renamed.end()) {
            new_key = known->second;
        } else if (moved_groups.contains(old_key.section('/', 0, 0))) {
            new_key = "av1/" + old_key;
        } else {
            continue;
        }
        if (!new_key.isEmpty() && !settings.contains(new_key)) {
            settings.setValue(new_key, settings.value(old_key));
        }
        settings.remove(old_key);
    }
}

}  // namespace fgw::app
