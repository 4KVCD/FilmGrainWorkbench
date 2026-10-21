"""Application entry point and theme."""

from __future__ import annotations

from pathlib import Path
import sys
import time
import traceback

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

STYLE = """
QGroupBox { font-weight: 600; border: 1px solid #3a4048; border-radius: 6px; margin-top: 10px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9fd3c7; }
QLabel#info { color: #a9b1ba; }
QLabel#warning { color: #1b1f24; background: #e3b341; border-radius: 4px; padding: 6px; }
QPushButton { padding: 5px 10px; }
QPushButton[primary="true"]:enabled { background: #2f8f6f; color: white; font-weight: 600; border: 1px solid #3fae88; border-radius: 4px; }
QPushButton[primary="true"]:enabled:hover { background: #37a57f; }
QPushButton[primary="true"]:disabled { background: #2a2e33; color: #6b737c; border: 1px solid #353a40; border-radius: 4px; }
QPushButton:checked { background: #3d6ea8; color: white; }
"""


def _dark_palette() -> QPalette:
    p = QPalette()
    base, window, text = QColor(30, 33, 37), QColor(38, 42, 47), QColor(225, 228, 232)
    disabled = QColor(110, 116, 124)
    for role, color in ((QPalette.ColorRole.Window, window), (QPalette.ColorRole.WindowText, text),
                        (QPalette.ColorRole.Base, base), (QPalette.ColorRole.AlternateBase, window),
                        (QPalette.ColorRole.ToolTipBase, QColor(50, 55, 61)),
                        (QPalette.ColorRole.ToolTipText, text), (QPalette.ColorRole.Text, text),
                        (QPalette.ColorRole.Button, QColor(48, 53, 59)), (QPalette.ColorRole.ButtonText, text),
                        (QPalette.ColorRole.Highlight, QColor(61, 110, 168)),
                        (QPalette.ColorRole.HighlightedText, QColor(255, 255, 255)),
                        (QPalette.ColorRole.Link, QColor(40, 30, 0))):
        p.setColor(role, color)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return p


def install_error_handler(window) -> None:
    """Report errors raised on the GUI thread instead of losing them.

    Under pythonw an exception in a Qt slot is printed to a stderr nobody sees
    and the window carries on half-updated. It is written to errors.log in the
    app's data folder, added to the activity log and shown in a message box.
    """
    showing = []

    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log = Path(window.data_dir) / "errors.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + text + "\n")
        except OSError:
            pass
        try:
            window.log("Unexpected error: " + text.strip().splitlines()[-1])
        except Exception:
            pass
        if showing:                       # an error while the box is open: log it only
            return
        showing.append(True)
        try:
            QMessageBox.critical(window, "Unexpected error",
                                 f"Something went wrong; the details are in {log}.\n\n"
                                 f"{text.strip().splitlines()[-1]}")
        finally:
            showing.clear()

    sys.excepthook = hook


def main() -> int:
    app = QApplication(sys.argv)
    # The application and organisation names place the data folder (saved analyses,
    # errors.log): they stay as they were. What the user sees is the display name.
    app.setApplicationName("AFGS1 Grain Workbench")
    app.setOrganizationName("AFGS1Workbench")
    app.setApplicationDisplayName("Film Grain Workbench")
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(STYLE)
    from .shell import AV1_TAB, VVC_TAB, Workbench
    window = Workbench()
    if "--vvc" in sys.argv[1:]:                    # open on a given tab, else the one used last
        window.show_tab(VVC_TAB)
    elif "--av1" in sys.argv[1:]:
        window.show_tab(AV1_TAB)
    install_error_handler(window)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
