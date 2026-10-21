// The QApplication that reports errors escaping the window's thread.
#pragma once

#include <QApplication>
#include <QPointer>
#include <QString>

namespace fgw::app {

class Workbench;

// Errors that escape a slot or an event handler are reported instead of
// ending the app: written to errors.log in the data folder, shown in the tab
// in view, and in a message box. (Work on job threads reports its own
// errors; this is for the window's thread.)
class Application : public QApplication {
public:
    using QApplication::QApplication;

    void set_window(Workbench* window) { window_ = window; }
    bool notify(QObject* receiver, QEvent* event) override;

private:
    void report(const QString& message);

    QPointer<Workbench> window_;
    bool showing_ = false;  // the message box is open
};

}  // namespace fgw::app
