// Where the app keeps its settings and data, and the settings both tabs share.
#pragma once

#include <QSettings>
#include <QString>

#include "fgw/tools.h"

namespace fgw::app {

inline const QString app_title = QStringLiteral("Film Grain Workbench");
// The organisation and application names place the settings (the registry
// key AFGS1Workbench\AFGS1Workbench) and the data folder (saved analyses,
// errors.log): they are the Python app's, so both are found again.
inline const QString organisation_name = QStringLiteral("AFGS1Workbench");
inline const QString application_name = QStringLiteral("AFGS1 Grain Workbench");
inline const QString settings_application = QStringLiteral("AFGS1Workbench");

// The settings in the registry, where the app keeps them. With the
// environment variable FGW_PROFILE set to a folder (for tests), they are in
// settings.ini there instead, and the data folder is its data subfolder.
QSettings* make_settings(QObject* parent);

// %APPDATA%\AFGS1Workbench\AFGS1 Grain Workbench: saved analyses and errors.log.
QString data_folder();

// The programs as set under Settings > External programs, else as found.
Tools load_tools(const QSettings& settings);
void save_tools(QSettings& settings, const Tools& tools);

// Moves the AV1 tab's settings from before the tabs (top-level keys) under
// av1/, once. tools/* and files/last_dir stay shared by both tabs.
void migrate_settings(QSettings& settings);

}  // namespace fgw::app
