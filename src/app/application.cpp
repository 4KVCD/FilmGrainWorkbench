#include "application.h"

#include <exception>
#include <fstream>

#include <QDateTime>
#include <QDir>
#include <QMessageBox>

#include "fgw/text.h"
#include "settings.h"
#include "workbench.h"

namespace fgw::app {

bool Application::notify(QObject* receiver, QEvent* event) {
    try {
        return QApplication::notify(receiver, event);
    } catch (const std::exception& error) {
        report(QString::fromUtf8(error.what()));
    } catch (...) {
        report("an unknown error");
    }
    return false;
}

void Application::report(const QString& message) {
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
                          "Something went wrong; the details are in " + QDir::toNativeSeparators(log) + ".\n\n" +
                              message);
    showing_ = false;
}

}  // namespace fgw::app
