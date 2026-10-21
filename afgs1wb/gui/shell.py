"""The app's one window: a tab for VVC's own film grain (first) and one for AV1 grain (AFGS1) in VVC encodes."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QMainWindow, QTabWidget

from .main_window import MainWindow
from .vvc_window import VvcGrainWindow

VVC_TAB, AV1_TAB = 0, 1


class Workbench(QMainWindow):
    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Film Grain Workbench")
        self.settings = settings or QSettings("AFGS1Workbench", "AFGS1Workbench")
        self.av1 = MainWindow(self.settings)
        self.vvc = VvcGrainWindow(self.settings)
        self.pages = (self.vvc, self.av1)              # in tab order
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for page, title, tip in (
                (self.vvc, "VVC grain analyzer (VVenC)",
                 "VVenC's own film grain analysis (--fga 1, 2, 3) and vvdec's synthesis, scene by scene."),
                (self.av1, "AV1 grain into VVC (AFGS1)",
                 "Measure the grain a VVC encode lost with libaom's noise model and write it as AFGS1 metadata.")):
            page.setWindowFlags(Qt.WindowType.Widget)       # a page, not a window of its own
            page.menuBar().hide()                           # its menu is this window's
            self.tabs.addTab(page, title)
            self.tabs.setTabToolTip(self.tabs.count() - 1, tip)
        self.setCentralWidget(self.tabs)
        settings_menu = self.menuBar().addMenu("&Settings")
        settings_menu.addAction("External programs…", self.av1.edit_tools)
        self.av1.tools_changed.connect(self._tools_changed)     # from the menu or the AV1 tab's banner
        if self.settings.value("shell/geometry"):
            self.restoreGeometry(self.settings.value("shell/geometry"))
        else:
            self.resize(1600, 1000)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.setCurrentIndex(int(self.settings.value("shell/tab", VVC_TAB)))
        self._tab_changed(self.tabs.currentIndex())

    @property
    def data_dir(self):                                     # for the error handler
        return self.av1.data_dir

    def log(self, text: str) -> None:
        """For the error handler: the message goes to the tab in view (and the AV1 tab's activity log)."""
        self.tabs.currentWidget().status.label.setText(text)
        if self.tabs.currentWidget() is self.av1:
            self.av1.log(text)

    def show_tab(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)

    def _tab_changed(self, index: int) -> None:
        # Both pages use the same keys (arrows, 1-4, zoom); only the visible one answers them.
        for i, page in enumerate(self.pages):
            for shortcut in page.findChildren(QShortcut):
                shortcut.setEnabled(i == index)
        self.settings.setValue("shell/tab", index)

    def _tools_changed(self) -> None:
        self.vvc.reload_tools()                             # the VVC tab uses the same ffmpeg / ffprobe

    def closeEvent(self, event) -> None:
        # Every tab is asked before any is shut down: the AV1 tab asks while it is writing,
        # and a "No" there must leave the VVC tab's work and files as they are.
        if not all(page.can_close() for page in self.pages):
            event.ignore()
            return
        for page in self.pages:
            page.shutdown()
        self.settings.setValue("shell/geometry", self.saveGeometry())
        event.accept()
