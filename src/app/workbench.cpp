#include "workbench.h"

#include <algorithm>

#include <QCloseEvent>
#include <QMenuBar>
#include <QShortcut>

#include "av1_tab.h"
#include "dialogs.h"
#include "settings.h"
#include "vvc_tab.h"

namespace fgw::app {

Workbench::Workbench(QSettings* settings, QWidget* parent)
    : QMainWindow(parent),
      settings_(settings),
      data_dir_(data_folder()),
      av1_(new Av1Tab(settings, data_dir_)),
      vvc_(new VvcTab(settings)),
      tabs_(new QTabWidget(this)) {
    setWindowTitle(app_title);
    tabs_->setDocumentMode(true);
    const struct {
        QMainWindow* page;
        const char* title;
        const char* tip;
    } pages[] = {
        {vvc_, "VVC grain analyzer (VVenC)",
         "VVenC's own film grain analysis (--fga 1, 2, 3) and vvdec's synthesis, scene by scene."},
        {av1_, "AV1 grain into VVC (AFGS1)",
         "Measure the grain a VVC encode lost with libaom's noise model and write it as AFGS1 metadata."},
    };
    for (const auto& [page, title, tip] : pages) {
        page->setWindowFlags(Qt::Widget);  // a page, not a window of its own
        page->menuBar()->hide();           // its menu is this window's
        tabs_->addTab(page, title);
        tabs_->setTabToolTip(tabs_->count() - 1, tip);
    }
    setCentralWidget(tabs_);

    QMenu* settings_menu = menuBar()->addMenu("&Settings");
    settings_menu->addAction("External programs…", av1_, &Av1Tab::edit_tools);
    QMenu* help_menu = menuBar()->addMenu("&Help");
    help_menu->addAction("Check installation…", this, &Workbench::check_installation);
    help_menu->addAction("About " + app_title, this, [this] { AboutDialog(this).exec(); });
    // From the menu or the AV1 tab's banner: the VVC tab uses the same ffmpeg and ffprobe.
    connect(av1_, &Av1Tab::tools_changed, vvc_, &VvcTab::reload_tools);

    if (const QByteArray geometry = settings_->value("shell/geometry").toByteArray(); !geometry.isEmpty()) {
        restoreGeometry(geometry);
    } else {
        resize(1600, 1000);
    }
    connect(tabs_, &QTabWidget::currentChanged, this, &Workbench::tab_changed);
    tabs_->setCurrentIndex(std::clamp(settings_->value("shell/tab", int{vvc_tab}).toInt(), 0, tabs_->count() - 1));
    tab_changed(tabs_->currentIndex());
}

void Workbench::log(const QString& text) {
    if (tabs_->currentWidget() == av1_) {
        av1_->status().set_text(text);
        av1_->log(text);
    } else {
        vvc_->status().set_text(text);
    }
}

void Workbench::tab_changed(int index) {
    // Both pages use the same keys (arrows, 1-4, zoom); only the visible one answers them.
    for (int i = 0; i < tabs_->count(); ++i) {
        for (QShortcut* shortcut : tabs_->widget(i)->findChildren<QShortcut*>()) {
            shortcut->setEnabled(i == index);
        }
    }
    settings_->setValue("shell/tab", index);
}

void Workbench::check_installation() { InstallationDialog(av1_->tools(), vvc_->vvc_tools(), this).exec(); }

void Workbench::closeEvent(QCloseEvent* event) {
    // Every tab is asked before any is shut down: the AV1 tab asks while it
    // is writing, and a "No" there must leave the VVC tab's work and files as
    // they are.
    if (!vvc_->can_close() || !av1_->can_close()) {
        event->ignore();
        return;
    }
    settings_->setValue("shell/geometry", saveGeometry());
    // Gone from the screen at once: deleting the VVC tab's scene files takes
    // about a second (33 frames at 4K).
    hide();
    vvc_->shutdown();
    av1_->shutdown();
    event->accept();
}

}  // namespace fgw::app
