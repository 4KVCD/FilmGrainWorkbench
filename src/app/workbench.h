// The app's one window: a tab for VVC's own film grain (first) and one for
// AV1 grain (AFGS1) in VVC encodes, with the Settings and Help menus.
#pragma once

#include <QMainWindow>
#include <QSettings>
#include <QTabWidget>

namespace fgw::app {

class Av1Tab;
class VvcTab;

class Workbench : public QMainWindow {
    Q_OBJECT

public:
    enum Tab { vvc_tab = 0, av1_tab = 1 };  // in tab order

    explicit Workbench(QSettings* settings, QWidget* parent = nullptr);

    void show_tab(Tab tab) { tabs_->setCurrentIndex(tab); }
    // For the error handler: the message goes to the tab in view (and the
    // AV1 tab's activity log).
    void log(const QString& text);
    const QString& data_dir() const { return data_dir_; }

    Av1Tab* av1() const { return av1_; }
    VvcTab* vvc() const { return vvc_; }

protected:
    void closeEvent(QCloseEvent* event) override;

private:
    void tab_changed(int index);
    void check_installation();

    QSettings* settings_;
    QString data_dir_;
    Av1Tab* av1_;
    VvcTab* vvc_;
    QTabWidget* tabs_;
};

}  // namespace fgw::app
