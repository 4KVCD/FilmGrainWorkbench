// The app's dialogs: the external programs, About, and Check installation.
#pragma once

#include <map>
#include <string>

#include <QDialog>
#include <QLabel>
#include <QLineEdit>
#include <QPlainTextEdit>

#include "fgw/tools.h"
#include "fgw/vvc_grain.h"

namespace fgw::app {

// Where ffmpeg, ffprobe and mkvmerge are (Settings > External programs).
class ToolsDialog : public QDialog {
    Q_OBJECT

public:
    ToolsDialog(const Tools& tools, QWidget* parent = nullptr);
    Tools tools() const;

private:
    void browse(const std::string& name);
    void find_automatically();
    void refresh();

    std::map<std::string, QLineEdit*> edits_;
    std::map<std::string, QLabel*> states_;
};

class AboutDialog : public QDialog {
    Q_OBJECT

public:
    explicit AboutDialog(QWidget* parent = nullptr);
};

// Each program the app runs, where it was found and its version. The checks
// run on a thread of their own; the report appears when they are done.
class InstallationDialog : public QDialog {
    Q_OBJECT

public:
    InstallationDialog(const Tools& tools, const VvcTools& vvc_tools, QWidget* parent = nullptr);

private:
    QPlainTextEdit* text_;
};

}  // namespace fgw::app
